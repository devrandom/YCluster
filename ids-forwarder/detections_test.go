package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDetectionRecordIsWritten(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	line := tcpLine(t, "/usr/bin/curl", "https://203.0.113.10/", "203.0.113.10", 8443)
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	f.closeState()
	if got := f.stats.recorded.Load(); got != 1 {
		t.Fatalf("recorded = %d, want 1", got)
	}

	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	var rec detectionRecord
	if err := json.Unmarshal(body, &rec); err != nil {
		t.Fatalf("detection line is not JSON: %v (%q)", err, body)
	}
	if rec.RuleID == "" || rec.Source != sourceTetragon || rec.Host != cfg.hostname {
		t.Fatalf("unexpected record: %+v", rec)
	}
	if rec.Time == "" || rec.Summary == "" {
		t.Fatalf("record is missing time or summary: %+v", rec)
	}
	if strings.Contains(rec.Summary, "203.0.113.10") {
		t.Fatalf("record keeps a destination IP: %q", rec.Summary)
	}
}

func TestSuppressedEventsAreNotRecorded(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	line := tcpLine(t, "/usr/bin/ceph", "health", "10.0.0.12", 3300)
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	f.closeState()
	if got := f.stats.recorded.Load(); got != 0 {
		t.Fatalf("recorded = %d, want 0 for a suppressed event", got)
	}
	if body, err := os.ReadFile(cfg.detectionsFile); err == nil && len(body) > 0 {
		t.Fatalf("suppressed event was recorded: %q", body)
	}
}

func TestDetectionLogRotates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "detections.jsonl")
	log, err := openDetectionLog(path, 400, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := log.close(); err != nil {
			t.Fatal(err)
		}
	}()
	for i := 0; i < 40; i++ {
		rec := detectionRecord{
			Time: "2024-01-01T00:00:00Z", Host: "h", Source: sourceTetragon,
			RuleID: "tcp-connect", Severity: "warning", Layer: "host",
			Summary: strings.Repeat("x", 50),
		}
		if err := log.append(rec); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("live detections file missing: %v", err)
	}
	if _, err := os.Stat(path + ".1"); err != nil {
		t.Fatalf("rotated detections file missing: %v", err)
	}
	// keep=2 means the live file plus .1 and .2, never .3.
	if _, err := os.Stat(path + ".3"); err == nil {
		t.Fatal("rotated file beyond the keep count was retained")
	}
	for _, name := range []string{path, path + ".1", path + ".2"} {
		info, err := os.Stat(name)
		if err != nil {
			continue
		}
		if info.Size() > 400 {
			t.Fatalf("%s = %d bytes, want <= 400", name, info.Size())
		}
	}
}
