package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func testConfig() config {
	cfg := defaultConfig()
	cfg.hostname = "test-host"
	cfg.alertTTL = time.Minute
	return cfg
}

func TestRedactIPs(t *testing.T) {
	in := "connect 10.0.0.25 -> 203.0.113.9 and 2001:db8::1"
	got := redactIPs(in)
	if strings.Contains(got, "10.0.0.25") || strings.Contains(got, "203.0.113.9") || strings.Contains(got, "2001:db8::1") {
		t.Fatalf("IP survived redaction: %q", got)
	}
	if strings.Count(got, "[ip]") != 3 {
		t.Fatalf("expected 3 redactions, got %q", got)
	}
}

func TestTetragonAlertMappingAndRedaction(t *testing.T) {
	cfg := testConfig()
	line := `{"process_kprobe":{"policy_name":"sensitive-file","message":"open /etc/shadow from 10.0.0.7","process":{"binary":"/usr/bin/cat","arguments":"-e"}},"node_name":"n1","time":"2026-10-25T00:00:00Z"}`
	a, ok := tetragonAlert(line, cfg)
	if !ok {
		t.Fatal("expected event to be forwarded")
	}
	if a.Labels["rule_id"] != "sensitive-file" || a.Labels["host"] != "test-host" || a.Labels["source"] != "tetragon" {
		t.Fatalf("unexpected labels: %v", a.Labels)
	}
	if strings.Contains(a.Annotations["summary"], "10.0.0.7") {
		t.Fatalf("IP leaked into summary: %q", a.Annotations["summary"])
	}
	if a.EndsAt == "" || a.StartsAt == "" {
		t.Fatal("missing timestamps")
	}
}

func TestTetragonEventFilter(t *testing.T) {
	cfg := testConfig()
	line := `{"process_exec":{"process":{"binary":"/bin/true"}}}`
	if _, ok := tetragonAlert(line, cfg); ok {
		t.Fatal("process_exec should be filtered by default config")
	}
	cfg.events = "process_exec"
	if _, ok := tetragonAlert(line, cfg); !ok {
		t.Fatal("process_exec should forward when configured")
	}
}

func TestCrowdsecAlertMapping(t *testing.T) {
	cfg := testConfig()
	item := map[string]any{
		"scenario": "ssh_bruteforce",
		"message":  "bf from 203.0.113.5",
		"severity": "critical",
	}
	a := crowdsecAlert(item, cfg)
	if a.Labels["source"] != "crowdsec" || a.Labels["rule_id"] != "ssh_bruteforce" {
		t.Fatalf("unexpected labels: %v", a.Labels)
	}
	if strings.Contains(a.Annotations["summary"], "203.0.113.5") {
		t.Fatalf("IP leaked: %q", a.Annotations["summary"])
	}
}

func TestQueueBoundedDropOldest(t *testing.T) {
	cfg := testConfig()
	cfg.queueMax = 2
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		f.queue.push(newAlert(cfg, sourceTetragon, "warning", "host", "r", "m"))
	}
	if f.queue.depth() != 2 {
		t.Fatalf("expected depth 2, got %d", f.queue.depth())
	}
	if _, ok := f.queue.pop(); !ok {
		t.Fatal("expected pop")
	}
}

func TestDedupWindow(t *testing.T) {
	d := newDedup(time.Minute)
	key := "k"
	if d.recent(key) {
		t.Fatal("fresh key should not be recent")
	}
	d.mark(key)
	if !d.recent(key) {
		t.Fatal("marked key should be recent")
	}
}

func TestPostSendsToAlertmanager(t *testing.T) {
	var got []map[string]any
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path != "/api/v2/alerts" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	cfg := testConfig()
	cfg.alertmanagerURL = srv.URL
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.post([]alert{newAlert(cfg, sourceTetragon, "warning", "host", "r", "s")}); err != nil {
		t.Fatalf("post: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 {
		t.Fatalf("expected 1 alert, got %d", len(got))
	}
}

func TestQueuePushFrontRejectsWhenFull(t *testing.T) {
	cfg := testConfig()
	cfg.queueMax = 1
	f, _ := newForwarder(cfg)
	a := newAlert(cfg, sourceTetragon, "warning", "host", "r", "one")
	b := newAlert(cfg, sourceTetragon, "warning", "host", "r", "two")
	if !f.queue.push(a) || !f.queue.push(b) {
		t.Fatal("pushes should succeed with drop-oldest")
	}
	if f.queue.pushFront(newAlert(cfg, sourceTetragon, "warning", "host", "r", "x")) {
		t.Fatal("pushFront should reject when full")
	}
}
