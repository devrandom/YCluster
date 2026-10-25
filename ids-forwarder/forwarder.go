package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	sourceTetragon = "tetragon"
	sourceCrowdSec = "crowdsec"
	alertName      = "IDSAlert"
)

type config struct {
	alertmanagerURL string
	tetragonLog     string
	events          string
	httpListen      string
	metricsListen   string
	hostname        string
	queueMax        int
	dedupWindow     time.Duration
	alertTTL        time.Duration
	postTimeout     time.Duration
}

func defaultConfig() config {
	return config{
		alertmanagerURL: "http://127.0.0.1:9093",
		tetragonLog:     "/var/log/tetragon/tetragon.log",
		events:          "process_kprobe",
		httpListen:      "",
		metricsListen:   "127.0.0.1:2113",
		queueMax:        1000,
		dedupWindow:     5 * time.Minute,
		alertTTL:        10 * time.Minute,
		postTimeout:     5 * time.Second,
	}
}

type forwarder struct {
	cfg    config
	queue  alertQueue
	dedup  dedup
	client *http.Client
	stats  metrics
}

func newForwarder(cfg config) (*forwarder, error) {
	if cfg.queueMax < 1 {
		return nil, errors.New("queue-max must be >= 1")
	}
	if cfg.dedupWindow < 0 {
		return nil, errors.New("dedup-window must be >= 0")
	}
	return &forwarder{
		cfg:    cfg,
		queue:  alertQueue{max: cfg.queueMax},
		dedup:  newDedup(cfg.dedupWindow),
		client: &http.Client{Timeout: cfg.postTimeout},
	}, nil
}

func (f *forwarder) run() error {
	if f.cfg.tetragonLog != "" {
		if err := f.startTailer(); err != nil {
			return err
		}
	}
	if f.cfg.httpListen != "" {
		if err := f.startReceiver(); err != nil {
			return err
		}
	}
	if f.cfg.metricsListen != "" {
		if err := f.startMetrics(); err != nil {
			return err
		}
	}
	f.postLoop()
	return nil
}

// startTailer tails the Tetragon JSON events file, forwarding one alert per
// line. It reopens from the beginning on rotation and starts at the end so a
// restart does not replay history (Tetragon keeps the authoritative copy).
func (f *forwarder) startTailer() error {
	file, _, err := tailOpen(f.cfg.tetragonLog)
	if err != nil {
		return fmt.Errorf("open %s: %w", f.cfg.tetragonLog, err)
	}
	go func() {
		buf := make([]byte, 0, 64*1024)
		scanner := newLineReader(file, buf)
		for {
			line, ok := scanner.next()
			if !ok {
				time.Sleep(500 * time.Millisecond)
				nf, rotated, err := tailOpen(f.cfg.tetragonLog)
				if err != nil {
					log.Printf("tetragon tail: %v", err)
					f.stats.dropped.Add(1)
					continue
				}
				scanner.close()
				scanner = newLineReader(nf, buf)
				_ = rotated
				continue
			}
			f.handleLine(string(line))
		}
	}()
	return nil
}

func (f *forwarder) handleLine(line string) {
	line = strings.TrimSpace(line)
	if line == "" {
		return
	}
	alert, ok := tetragonAlert(line, f.cfg)
	if !ok {
		return
	}
	f.enqueue(alert)
}

func (f *forwarder) enqueue(a alert) {
	key := a.dedupKey()
	if f.dedup.recent(key) {
		f.stats.deduped.Add(1)
		return
	}
	if !f.queue.push(a) {
		f.stats.dropped.Add(1)
		log.Printf("queue full, dropping oldest alert")
		return
	}
	f.dedup.mark(key)
}

func (f *forwarder) postLoop() {
	for {
		a, ok := f.queue.pop()
		if !ok {
			time.Sleep(500 * time.Millisecond)
			continue
		}
		if err := f.post([]alert{a}); err != nil {
			log.Printf("post to alertmanager: %v", err)
			f.stats.failures.Add(1)
			// Re-queue at the front unless the queue is full.
			if !f.queue.pushFront(a) {
				f.stats.dropped.Add(1)
			}
			time.Sleep(2 * time.Second)
			continue
		}
		f.stats.forwarded.Add(1)
	}
}

func (f *forwarder) post(alerts []alert) error {
	if len(alerts) == 0 {
		return nil
	}
	body, err := json.Marshal(alerts)
	if err != nil {
		return err
	}
	url := strings.TrimRight(f.cfg.alertmanagerURL, "/") + "/api/v2/alerts"
	ctx, cancel := context.WithTimeout(context.Background(), f.cfg.postTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := f.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("alertmanager returned %s", resp.Status)
	}
	return nil
}

// startReceiver runs the optional localhost HTTP receiver used by the
// CrowdSec notification-http plugin.
func (f *forwarder) startReceiver() error {
	mux := http.NewServeMux()
	mux.HandleFunc("/crowdsec", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var payload []map[string]any
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&payload); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		for _, item := range payload {
			f.enqueue(crowdsecAlert(item, f.cfg))
		}
		w.WriteHeader(http.StatusOK)
	})
	srv := &http.Server{Addr: f.cfg.httpListen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", f.cfg.httpListen)
	if err != nil {
		return err
	}
	if isNotLoopback(ln.Addr()) {
		ln.Close()
		return errors.New("http-listen must bind to a loopback address")
	}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("receiver: %v", err)
			os.Exit(1)
		}
	}()
	return nil
}

func isNotLoopback(addr net.Addr) bool {
	host, _, err := net.SplitHostPort(addr.String())
	if err != nil {
		return true
	}
	ip := net.ParseIP(host)
	return ip == nil || !ip.IsLoopback()
}

func (f *forwarder) startMetrics() error {
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintf(w, "ids_forwarder_up 1\n")
		fmt.Fprintf(w, "ids_forwarder_queue_depth %d\n", f.queue.depth())
		fmt.Fprintf(w, "ids_forwarder_forwarded_total %d\n", f.stats.forwarded.Load())
		fmt.Fprintf(w, "ids_forwarder_dropped_total %d\n", f.stats.dropped.Load())
		fmt.Fprintf(w, "ids_forwarder_deduped_total %d\n", f.stats.deduped.Load())
		fmt.Fprintf(w, "ids_forwarder_post_failures_total %d\n", f.stats.failures.Load())
	})
	srv := &http.Server{Addr: f.cfg.metricsListen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", f.cfg.metricsListen)
	if err != nil {
		return err
	}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("metrics: %v", err)
		}
	}()
	return nil
}
