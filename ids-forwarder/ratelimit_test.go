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
		// Only a class that may page reaches the limiter at all.
		a.Labels["escalation"] = "nonroot"
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
		// Only a class that may page reaches the limiter, so the cap being
		// disabled is only observable on a paging alert.
		a.Labels["escalation"] = "nonroot"
		if err := f.enqueue(a); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 9 {
		t.Fatalf("queued %d alerts, want all 9 with the cap disabled", got)
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
		a.Labels["escalation"] = "nonroot"
		if err := f.enqueue(a); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 2 {
		t.Fatalf("queued %d alerts, want one per severity", got)
	}
	warning := newAlert(cfg, sourceTetragon, "warning", "host", "another-warning", "process_exec: binary=/usr/bin/wget args=0")
	warning.Labels["escalation"] = "nonroot"
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

func TestAuditFindingsDoNotConsumeTheCap(t *testing.T) {
	cfg := testConfig()
	cfg.rateLimit = 1
	cfg.rateWindow = time.Hour
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	// Audit findings are recorded and never posted, so they leave the enqueue
	// path before the limiter and cannot take the window's only slot.
	for i := 0; i < 5; i++ {
		audit := newAlert(cfg, sourceTetragon, "warning", "host", "container-api",
			fmt.Sprintf("process_exec: binary=/usr/bin/incus args=list-%d", i))
		audit.Labels["container"] = ""
		if err := f.enqueue(audit); err != nil {
			t.Fatal(err)
		}
	}
	page := newAlert(cfg, sourceTetragon, "warning", "host", "privilege-raises",
		"process_kprobe: binary=/usr/bin/sudo")
	page.Labels["escalation"] = "nonroot"
	if err := f.enqueue(page); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queued %d alerts, want the one paging detection", got)
	}
	if got := f.stats.rateLimited.Load(); got != 0 {
		t.Fatalf("rate limited %d, want 0: audit findings must not take the slot", got)
	}
}
