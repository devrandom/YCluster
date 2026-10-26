package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestNewForwarderRejectsBadConfig(t *testing.T) {
	cfg := testConfig()
	cfg.queueMax = 0
	if _, err := newForwarder(cfg); err == nil {
		t.Fatal("queueMax=0 must be rejected")
	}
	cfg = testConfig()
	cfg.dedupWindow = -time.Second
	if _, err := newForwarder(cfg); err == nil {
		t.Fatal("negative dedupWindow must be rejected")
	}
}

func TestQueueRejectsWhenMaxIsZero(t *testing.T) {
	q := alertQueue{max: 0}
	if q.push(alert{}) {
		t.Fatal("push into a zero-capacity queue must fail")
	}
}

func TestIsPort(t *testing.T) {
	if isPort("") || isPort("123456") || isPort("12a") {
		t.Fatal("non-numeric or oversized ports must be rejected")
	}
	if !isPort("8080") {
		t.Fatal("numeric port must be accepted")
	}
}

func TestRedactTokenEdgeCases(t *testing.T) {
	if got := redactToken(""); got != "" {
		t.Fatalf("empty token: %q", got)
	}
	if got := redactToken("[2001:db8::2]"); got != "[ip]" {
		t.Fatalf("bracketed ipv6: %q", got)
	}
	if got := redactToken("2001:db8:0:0:0:0:2:1"); got != "[ip]" {
		t.Fatalf("full ipv6: %q", got)
	}
	if got := redactToken("::ffff:203.0.113.4"); got != "[ip]" {
		t.Fatalf("mapped ipv4: %q", got)
	}
	if got := redactToken("example.com"); got != "example.com" {
		t.Fatalf("hostname must survive: %q", got)
	}
	// A version-looking token must not be mangled.
	if got := redactToken("v1.2.3"); got != "v1.2.3" {
		t.Fatalf("version string must survive: %q", got)
	}
	_ = netip.Addr{}
}

func TestTetragonLayerAndSeverity(t *testing.T) {
	cfg := testConfig()
	// Pod metadata on the process marks a container event.
	line := `{"process_kprobe":{"policy_name":"p","process":{"binary":"/bin/sh","pod":{"namespace":"ns","name":"vm1"}}}}`
	a, ok := tetragonAlert(line, cfg)
	if !ok || a.Labels["layer"] != "container" {
		t.Fatalf("expected container layer, got %+v", a.Labels)
	}
	// A "kill" message escalates to critical.
	line = `{"process_kprobe":{"policy_name":"p","message":"SIGKILL sent","process":{"binary":"/bin/kill"}}}`
	a, ok = tetragonAlert(line, cfg)
	if !ok || a.Labels["severity"] != "critical" {
		t.Fatalf("expected critical severity, got %+v", a.Labels)
	}
}

func TestEventTypeVariants(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec,process_exit,process_connect"
	for _, line := range []string{
		`{"process_exec":{"process":{"binary":"/bin/a"}}}`,
		`{"process_exit":{"process":{"binary":"/bin/a"},"status":0}}`,
		`{"process_connect":{"process":{"binary":"/bin/a"}}}`,
	} {
		if _, ok := tetragonAlert(line, cfg); !ok {
			t.Fatalf("expected %s to forward", cfg.events)
		}
	}
	// Malformed JSON is silently dropped.
	if _, ok := tetragonAlert("{oops", cfg); ok {
		t.Fatal("malformed json must not forward")
	}
}

func TestSeenAndMarkExpiryBoundsMemory(t *testing.T) {
	d := newDedup(time.Minute)
	// Deterministically backdate 4200 keys past their window so the
	// cleanup pass evicts them, regardless of test-runner timing.
	d.mu.Lock()
	for i := 0; i < 4200; i++ {
		d.seen[fmt.Sprintf("k%d", i)] = time.Now().Add(-2 * time.Minute)
	}
	d.mu.Unlock()
	// The next insert triggers the cleanup pass at >4096.
	d.seenAndMark("trigger")
	// All expired keys must be evicted; only "trigger" remains.
	if got := len(d.seen); got > 1 {
		t.Fatalf("expected eviction to leave at most 1 entry, got %d", got)
	}
}

func TestStartReceiverBadJSON(t *testing.T) {
	cfg := testConfig()
	cfg.httpListen = "127.0.0.1:0"
	f, _ := newForwarder(cfg)
	if err := f.startReceiver(); err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post("http://"+f.receiverAddr()+"/crowdsec", "application/json", strings.NewReader("{bad"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", resp.StatusCode)
	}
	if f.queue.depth() != 0 {
		t.Fatal("bad payload must not queue alerts")
	}
}

func TestStartTailerMissingFileRetries(t *testing.T) {
	cfg := testConfig()
	cfg.tetragonLog = filepath.Join(t.TempDir(), "missing.log")
	f, _ := newForwarder(cfg)
	// A missing file is retryable (Tetragon creates it on first run), so
	// startTailer must succeed and wait for it.
	if err := f.startTailer(); err != nil {
		t.Fatalf("missing file should defer to the retry tailer: %v", err)
	}
	f.shutdown()
}

func TestStartTailerWithRetryPicksUpNewFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "late.log")
	var mu sync2Mutex
	var got []string
	tl := startTailerWithRetry(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, string(line))
	})
	defer tl.close()
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(path, []byte("late\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || got[0] != "late" {
		t.Fatalf("retry tailer missed the file: %v", got)
	}
}

func TestRunWithStopsCleanly(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(logPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.metricsListen = "127.0.0.1:0"
	cfg.pollInterval = 5 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- runWith(ctx, cfg) }()

	// Give the components time to start, then cancel and verify runWith
	// returns promptly (the shutdown path actually unblocks).
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runWith returned error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("runWith did not return after context cancel")
	}
}

func TestCrowdsecJSONRoundTrip(t *testing.T) {
	// Ensure the typed model decodes the real wire shape.
	var items []crowdsecAlert
	body := `[{"scenario":"bf","machine":{"hostname":"h"},"source":{"ip":"203.0.113.9","range":"203.0.113.0/24"},"events":[{"message":"m"}]}]`
	if err := json.Unmarshal([]byte(body), &items); err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Scenario != "bf" || items[0].Source.IP != "203.0.113.9" {
		t.Fatalf("decode: %+v", items)
	}
}

// sync2Mutex is an alias to keep the import list tidy in this file.
type sync2Mutex = sync.Mutex
