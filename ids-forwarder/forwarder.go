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
	"sync"
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
	execBinaries    string
	httpListen      string
	metricsListen   string
	hostname        string
	queueMax        int
	dedupWindow     time.Duration
	alertTTL        time.Duration
	postTimeout     time.Duration
	pollInterval    time.Duration
	logf            func(format string, args ...any)
}

func defaultConfig() config {
	return config{
		alertmanagerURL: "http://127.0.0.1:9093",
		tetragonLog:     "/var/log/tetragon/tetragon.log",
		events:          "process_kprobe",
		execBinaries:    "bash,sh,dash,zsh,fish,sudo,su,apt,apt-get,dpkg,snap,curl,wget",
		httpListen:      "",
		metricsListen:   "127.0.0.1:2113",
		queueMax:        1000,
		dedupWindow:     5 * time.Minute,
		alertTTL:        10 * time.Minute,
		postTimeout:     5 * time.Second,
		pollInterval:    500 * time.Millisecond,
		logf:            log.Printf,
	}
}

type forwarder struct {
	cfg    config
	queue  alertQueue
	dedup  dedup
	client *http.Client
	stats  metrics
	stop   chan struct{}
	once   sync.Once

	// wg tracks background goroutines (tailer, post loop) so tests can
	// coordinate shutdown.
	wg sync.WaitGroup

	receiverListen string
	metricsBind    string
}

func newForwarder(cfg config) (*forwarder, error) {
	if cfg.queueMax < 1 {
		return nil, errors.New("queue-max must be >= 1")
	}
	if cfg.dedupWindow < 0 {
		return nil, errors.New("dedup-window must be >= 0")
	}
	if cfg.logf == nil {
		cfg.logf = log.Printf
	}
	return &forwarder{
		cfg:    cfg,
		queue:  alertQueue{max: cfg.queueMax},
		dedup:  newDedup(cfg.dedupWindow),
		client: &http.Client{Timeout: cfg.postTimeout},
		stop:   make(chan struct{}),
	}, nil
}

// shutdown signals all loops to exit exactly once.
func (f *forwarder) shutdown() {
	f.once.Do(func() { close(f.stop) })
}

// runWith starts every configured component and blocks until ctx is
// cancelled or shutdown is requested. This is the production entry point
// after flag parsing.
func runWith(ctx context.Context, cfg config) error {
	f, err := newForwarder(cfg)
	if err != nil {
		return err
	}
	if err := f.start(); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		f.shutdown()
		f.wg.Wait()
		return nil
	case <-f.stop:
		f.wg.Wait()
		return nil
	}
}

// start launches the tailer, receiver, metrics and post loop according to
// cfg. It returns once all components are running.
func (f *forwarder) start() error {
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
	f.wg.Add(1)
	go f.postLoop()
	return nil
}

// receiverAddr returns the address the CrowdSec receiver is listening on,
// or "" when the receiver is disabled. Used by tests.
func (f *forwarder) receiverAddr() string { return f.receiverListen }

// metricsAddr returns the address the metrics server is listening on, or
// "" when metrics are disabled. Used by tests.
func (f *forwarder) metricsAddr() string { return f.metricsBind }

// startTailer tails the Tetragon JSON events file, forwarding one alert per
// line. The tailer tracks the read offset across EOF, buffers partial
// lines, and handles rotation and truncation. It starts at the end so a
// restart does not replay history (Tetragon keeps the authoritative copy).
// If the file does not exist yet, the tailer retries until it appears.
func (f *forwarder) startTailer() error {
	tl, err := newTailer(f.cfg.tetragonLog, 500*time.Millisecond, func(line []byte) {
		f.handleLine(string(line))
	})
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("open %s: %w", f.cfg.tetragonLog, err)
		}
		// File may not exist until Tetragon first runs; retry in the
		// background and require only that the directory exists.
		tl = startTailerWithRetry(f.cfg.tetragonLog, f.cfg.pollInterval, func(line []byte) {
			f.handleLine(string(line))
		})
	}
	go tl.run()
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
	if f.dedup.seenAndMark(key) {
		f.stats.deduped.Add(1)
		return
	}
	if !f.queue.push(a) {
		f.stats.dropped.Add(1)
		f.cfg.logf("queue full, dropping oldest alert")
		return
	}
}

func (f *forwarder) postLoop() {
	defer f.wg.Done()
	idle := f.cfg.pollInterval
	retry := idle * 4
	for {
		a, ok := f.queue.pop()
		if ok && a.expired(time.Now()) {
			f.stats.expired.Add(1)
			f.cfg.logf("dropping expired alert %s", a.Labels["rule_id"])
			continue
		}
		if !ok {
			select {
			case <-f.stop:
				return
			case <-time.After(idle):
				continue
			}
		}
		if err := f.post([]alert{a}); err != nil {
			f.cfg.logf("post to alertmanager: %v", err)
			// Re-queue at the front unless the queue is full.
			if !f.queue.pushFront(a) {
				f.stats.dropped.Add(1)
			}
			select {
			case <-f.stop:
				return
			case <-time.After(retry):
				continue
			}
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
	// alertmanager-url may be a comma-separated list (the cluster runs
	// Alertmanager on every core node). Try each in order; a single
	// reachable instance is enough for the alert to be accepted.
	var lastErr error
	for _, base := range strings.Split(f.cfg.alertmanagerURL, ",") {
		base = strings.TrimSpace(base)
		if base == "" {
			continue
		}
		if err := f.postOne(base, body); err != nil {
			lastErr = err
			continue
		}
		return nil
	}
	f.stats.failures.Add(1)
	if lastErr == nil {
		lastErr = errors.New("no alertmanager URL configured")
	}
	return lastErr
}

func (f *forwarder) postOne(base string, body []byte) error {
	url := strings.TrimRight(base, "/") + "/api/v2/alerts"
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
		var payload []crowdsecAlert
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&payload); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		for _, item := range payload {
			f.enqueue(crowdsecToAlert(item, f.cfg))
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
	f.receiverListen = ln.Addr().String()
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			f.cfg.logf("receiver: %v", err)
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
		fmt.Fprintf(w, "ids_forwarder_expired_total %d\n", f.stats.expired.Load())
	})
	srv := &http.Server{Addr: f.cfg.metricsListen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", f.cfg.metricsListen)
	if err != nil {
		return err
	}
	if isNotLoopback(ln.Addr()) {
		ln.Close()
		return errors.New("metrics-listen must bind to a loopback address")
	}
	f.metricsBind = ln.Addr().String()
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			f.cfg.logf("metrics: %v", err)
		}
	}()
	return nil
}
