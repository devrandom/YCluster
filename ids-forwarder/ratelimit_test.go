package main

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestRateLimitCapsDeliveryButKeepsRecords(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.rateLimit = 5
	cfg.rateWindow = time.Hour
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	for i := 0; i < 7; i++ {
		a := newAlert(cfg, sourceTetragon, "warning", "host", fmt.Sprintf("rule-%d", i),
			fmt.Sprintf("process_exec: binary=/usr/bin/curl args=%d", i))
		if err := f.enqueue(a); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 5 {
		t.Fatalf("queued %d alerts, want 5", got)
	}
	if got := f.stats.rateLimited.Load(); got != 2 {
		t.Fatalf("rate limited %d, want 2", got)
	}
	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 7 {
		t.Fatalf("recorded %d detections, want all 7", n)
	}
}

func TestRateLimitZeroDisablesTheCap(t *testing.T) {
	cfg := testConfig()
	cfg.rateLimit = 0
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	for i := 0; i < 9; i++ {
		a := newAlert(cfg, sourceTetragon, "warning", "host", "rule", fmt.Sprintf("process_exec: binary=/bin/sh args=%d", i))
		if err := f.enqueue(a); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.stats.rateLimited.Load(); got != 0 {
		t.Fatalf("rate limited %d with the cap disabled", got)
	}
}

func TestRateLimitIsPerSeverity(t *testing.T) {
	r := newRateLimiter(2, time.Hour)
	for i := 0; i < 2; i++ {
		if !r.allow("warning") {
			t.Fatalf("warning %d should be allowed", i+1)
		}
	}
	if r.allow("warning") {
		t.Fatal("third warning should be refused")
	}
	if !r.allow("critical") {
		t.Fatal("critical budget must be independent of warnings")
	}
}

func TestRateLimitPerSeverityInForwarder(t *testing.T) {
	cfg := testConfig()
	cfg.rateLimit = 1
	cfg.rateWindow = time.Hour
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	for _, severity := range []string{"warning", "critical"} {
		a := newAlert(cfg, sourceTetragon, severity, "host", "rule-"+severity, "process_exec: binary=/usr/bin/curl args=0")
		if err := f.enqueue(a); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 2 {
		t.Fatalf("queued %d alerts, want one per severity", got)
	}
	warning := newAlert(cfg, sourceTetragon, "warning", "host", "another-warning", "process_exec: binary=/usr/bin/wget args=0")
	if err := f.enqueue(warning); err != nil {
		t.Fatal(err)
	}
	if got := f.stats.rateLimited.Load(); got != 1 {
		t.Fatalf("rate limited %d, want 1", got)
	}
}

func TestRateLimitWindowExpires(t *testing.T) {
	r := newRateLimiter(2, 50*time.Millisecond)
	if !r.allow("warning") || !r.allow("warning") {
		t.Fatal("first two alerts should be allowed")
	}
	if r.allow("warning") {
		t.Fatal("third alert inside the window should be refused")
	}
	time.Sleep(60 * time.Millisecond)
	if !r.allow("warning") {
		t.Fatal("alert after the window should be allowed")
	}
}

func TestAuditRuleDoesNotConsumeTheCap(t *testing.T) {
	cfg := testConfig()
	cfg.rateLimit = 1
	cfg.rateWindow = time.Hour
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	audit := newAlert(cfg, sourceTetragon, "warning", "host", "container-api", "process_exec: binary=/usr/bin/incus args=list")
	audit.Labels["container"] = ""
	if err := f.enqueue(audit); err != nil {
		t.Fatal(err)
	}
	real := newAlert(cfg, sourceTetragon, "warning", "host", "tcp-connect", "process_kprobe: binary=/usr/bin/curl")
	if err := f.enqueue(real); err != nil {
		t.Fatal(err)
	}
	if got := f.stats.rateLimited.Load(); got != 0 {
		t.Fatalf("rate limited %d, want 0: the audit rule must not take the slot", got)
	}
}
