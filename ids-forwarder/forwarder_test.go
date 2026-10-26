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
	cases := []struct{ in, want string }{
		{"connect 10.0.0.25 -> 203.0.113.9", "connect [ip] -> [ip]"},
		{"v6 compressed 2001:db8::1", "v6 compressed [ip]"},
		{"v6 full 2001:db8:0:0:0:0:2:1", "v6 full [ip]"},
		{"bracketed [fe80::1%eth0]", "bracketed [ip]"},
		{"mapped ::ffff:203.0.113.4", "mapped [ip]"},
		{"host 10.0.0.5:8080", "host [ip]:8080"},
		{"not an ip 2001:db8", "not an ip 2001:db8"},
		{"no ips here", "no ips here"},
		{"cidr 203.0.113.5/32", "cidr [ip]/32"},
		{"cidr v6 2001:db8::/64", "cidr v6 [ip]/64"},
		{"url http://203.0.113.5/path", "url http://[ip]/path"},
		{"url6 http://[2001:db8::1]:443/x", "url6 http://[ip]:443/x"},
		{"bracketed port [2001:db8::1]:443", "bracketed port [ip]:443"},
	}
	for _, c := range cases {
		if got := redactIPs(c.in); got != c.want {
			t.Errorf("redactIPs(%q) = %q, want %q", c.in, got, c.want)
		}
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
	// Shape mirrors CrowdSec 1.8.x models.Alert as posted by the
	// notification-http plugin ({{.|toJson}}).
	item := crowdsecAlert{
		Scenario: "ssh_bruteforce",
		Machine:  &crowdsecMachine{Hostname: "node1"},
		Source:   &crowdsecSource{IP: "203.0.113.5", Range: "203.0.113.0/24"},
		Events: []crowdsecEvent{
			{Message: "ssh bruteforce attempt from 203.0.113.5"},
		},
	}
	a := crowdsecToAlert(item, cfg)
	if a.Labels["source"] != "crowdsec" || a.Labels["rule_id"] != "ssh_bruteforce" {
		t.Fatalf("unexpected labels: %v", a.Labels)
	}
	if a.Labels["severity"] != "warning" {
		t.Fatalf("crowdsec alerts carry no severity; expected fixed warning, got %q", a.Labels["severity"])
	}
	if strings.Contains(a.Annotations["summary"], "203.0.113.5") {
		t.Fatalf("IP leaked: %q", a.Annotations["summary"])
	}
	if !strings.Contains(a.Annotations["summary"], "source=present") {
		t.Fatalf("expected source marker in summary: %q", a.Annotations["summary"])
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
	if d.seenAndMark(key) {
		t.Fatal("fresh key should not be recent")
	}
	if !d.seenAndMark(key) {
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
