package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestForwarderEndToEnd(t *testing.T) {
	var mu sync.Mutex
	var received []map[string]any
	srv := httptestAlertmanager(t, func(body []byte) {
		mu.Lock()
		defer mu.Unlock()
		_ = json.Unmarshal(body, &received)
	})

	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(logPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}

	cfg := testConfig()
	cfg.alertmanagerURL = srv
	cfg.tetragonLog = logPath
	cfg.events = "process_kprobe,process_exec"
	cfg.metricsListen = "127.0.0.1:0"
	cfg.pollInterval = 20 * time.Millisecond
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.start(); err != nil {
		t.Fatal(err)
	}
	defer f.shutdown()

	// Append an event the forwarder is configured to relay.
	f2, _ := os.OpenFile(logPath, os.O_APPEND|os.O_WRONLY, 0o644)
	// A sudo exec from a non-root parent is escalation_nonroot, which is a
	// class that pages; Alertmanager only ever sees those.
	f2.WriteString(`{"process_exec":{"process":{"pid":4242,"uid":0,"binary":"/usr/bin/sudo","arguments":"-n id -u"},"parent":{"pid":4241,"uid":2000,"binary":"/bin/bash"}}}` + "\n")
	f2.Close()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(received)
		mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(received) == 0 {
		t.Fatal("alert never reached the Alertmanager stub")
	}
}

func TestPostLoopRequeuesOnFailure(t *testing.T) {
	cfg := testConfig()
	cfg.pollInterval = 10 * time.Millisecond
	f, _ := newForwarder(cfg)

	// Point at a closed port to force a POST failure.
	f.cfg.alertmanagerURL = "http://127.0.0.1:1"
	a := newAlert(cfg, sourceTetragon, "warning", "host", "r", "s")
	f.queue.push(a)

	f.wg.Add(1)
	go f.postLoop()

	// The post loop should attempt delivery, fail, re-queue, and retry.
	// Wait for at least two failure attempts to prove the retry loop works.
	waitFor(t, 3*time.Second, func() bool {
		return f.stats.failures.Load() >= 2
	})
	if f.queue.depth() != 1 {
		t.Fatalf("alert should stay queued for retry, depth=%d", f.queue.depth())
	}
	if f.stats.forwarded.Load() != 0 {
		t.Fatalf("nothing should be forwarded to a dead sink: %d", f.stats.forwarded.Load())
	}
	f.shutdown()
	f.wg.Wait()
}

func TestPostLoopDropsExpiredBacklog(t *testing.T) {
	var mu sync.Mutex
	posts := 0
	srv := httptestAlertmanager(t, func([]byte) {
		mu.Lock()
		posts++
		mu.Unlock()
	})

	cfg := testConfig()
	cfg.alertmanagerURL = srv
	cfg.pollInterval = 10 * time.Millisecond
	cfg.maxEventAge = 10 * time.Minute
	f, _ := newForwarder(cfg)

	a := newAlert(cfg, sourceTetragon, "warning", "host", "r", "stale")
	a.eventTime = time.Now().Add(-20 * time.Minute)
	f.queue.push(a)

	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 3*time.Second, func() bool {
		return f.stats.expired.Load() == 1
	})
	f.shutdown()
	f.wg.Wait()

	if f.queue.depth() != 0 {
		t.Fatalf("expired alert must be removed from the queue, depth=%d", f.queue.depth())
	}
	mu.Lock()
	defer mu.Unlock()
	if posts != 0 {
		t.Fatalf("expired alert must not be posted, got %d posts", posts)
	}
}

func TestReceiverOnlyAcceptsLoopback(t *testing.T) {
	cfg := testConfig()
	cfg.httpListen = "0.0.0.0:0"
	f, _ := newForwarder(cfg)
	if err := f.startReceiver(); err == nil {
		t.Fatal("non-loopback bind should be rejected")
	}
}

func TestReceiverEndToEnd(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.httpListen = "127.0.0.1:0"
	cfg.spoolFile = filepath.Join(dir, "spool.jsonl")
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if err := f.startReceiver(); err != nil {
		t.Fatal(err)
	}
	addr := f.receiverAddr()
	if addr == "" {
		t.Fatal("receiver address not recorded")
	}
	resp, err := http.Post("http://"+addr+"/crowdsec", "application/json",
		strings.NewReader(`[{"scenario":"bf","machine":{"hostname":"h"},"events":[{"message":"m"}]}]`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("receiver status %d", resp.StatusCode)
	}
	// CrowdSec scenarios are a silent class: recorded, never delivered.
	if got := processCount(t, cfg.detectionsFile); got != 1 {
		t.Fatalf("recorded %d detections, want 1", got)
	}
	if got := f.spool.depth(); got != 0 {
		t.Fatalf("a silent class must not reach the spool; depth=%d", got)
	}
}

func TestReceiverRejectsNonPost(t *testing.T) {
	cfg := testConfig()
	cfg.httpListen = "127.0.0.1:0"
	f, _ := newForwarder(cfg)
	if err := f.startReceiver(); err != nil {
		t.Fatal(err)
	}
	resp, err := http.Get("http://" + f.receiverAddr() + "/crowdsec")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", resp.StatusCode)
	}
}

func TestMetricsEndpointLoopback(t *testing.T) {
	cfg := testConfig()
	cfg.metricsListen = "127.0.0.1:0"
	f, _ := newForwarder(cfg)
	if err := f.startMetrics(); err != nil {
		t.Fatal(err)
	}
	addr := f.metricsAddr()
	resp, err := http.Get("http://" + addr + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("metrics status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	// Every class gets a series even before it fires, so a quiet class is a
	// zero rather than a gap the gate cannot read.
	for _, class := range knownClasses {
		line := fmt.Sprintf("ids_forwarder_page_candidates_total{class=%q}", class)
		if !strings.Contains(text, line) {
			t.Fatalf("missing %s in the metrics output", line)
		}
	}
	if !strings.Contains(text, "ids_forwarder_record_rotations_total 0") {
		t.Fatal("record rotation counter is not exposed")
	}
}

func TestMetricsRejectsNonLoopback(t *testing.T) {
	cfg := testConfig()
	cfg.metricsListen = "0.0.0.0:0"
	f, _ := newForwarder(cfg)
	if err := f.startMetrics(); err == nil {
		t.Fatal("non-loopback metrics bind should be rejected")
	}
}

func TestHandleLineDropsUnconfigured(t *testing.T) {
	cfg := testConfig()
	f, _ := newForwarder(cfg)
	f.handleLine(`{"process_exec":{"process":{"binary":"/bin/true"}}}`)
	if f.queue.depth() != 0 {
		t.Fatal("unconfigured event should not queue")
	}
	f.handleLine("not json")
	if f.queue.depth() != 0 {
		t.Fatal("malformed line should not queue")
	}
	// A privilege raise is a class that pages, so it queues; a plain kprobe
	// would only be recorded.
	f.handleLine(`{"process_kprobe":{"policy_name":"privilege-raises","function_name":"__sys_setuid","process":{"pid":900,"uid":1000,"binary":"/usr/bin/sudo","ns":{"pid":{"inum":4026531836,"is_host":true},"mnt":{"inum":4026531841,"is_host":true}}},"args":[{"int_arg":0}]}}`)
	if f.queue.depth() != 1 {
		t.Fatalf("configured paging event should queue; depth=%d", f.queue.depth())
	}
}

func TestEnqueueDedupsRepeatedAlerts(t *testing.T) {
	cfg := testConfig()
	f, _ := newForwarder(cfg)
	a := pagingAlert(cfg, "warning", "host", "r", "same")
	f.enqueue(a)
	f.enqueue(a)
	if f.queue.depth() != 1 {
		t.Fatalf("dedup should suppress the repeat; depth=%d", f.queue.depth())
	}
	if f.stats.deduped.Load() != 1 {
		t.Fatalf("deduped counter=%d", f.stats.deduped.Load())
	}
}

func TestPostFailureSurfaces(t *testing.T) {
	cfg := testConfig()
	f, _ := newForwarder(cfg)
	f.cfg.alertmanagerURL = "http://127.0.0.1:1"
	if err := f.post([]alert{newAlert(cfg, sourceTetragon, "warning", "h", "r", "s")}); err == nil {
		t.Fatal("expected error posting to a closed port")
	}
	if f.stats.failures.Load() != 1 {
		t.Fatalf("failure counter=%d", f.stats.failures.Load())
	}
}

// --- helpers ---

// httptestAlertmanager starts a stub Alertmanager on a random loopback
// port and returns its base URL.
func httptestAlertmanager(t *testing.T, sink func([]byte)) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v2/alerts", func(w http.ResponseWriter, r *http.Request) {
		buf := make([]byte, 64*1024)
		n, _ := r.Body.Read(buf)
		sink(buf[:n])
		w.WriteHeader(http.StatusOK)
	})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { srv.Close() })
	return "http://" + ln.Addr().String()
}

var _ = fmt.Sprintf
