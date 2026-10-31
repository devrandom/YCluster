package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

const testSigmaEvent = `{"time":"2026-10-28T12:00:00Z","process_exec":{"process":{"pid":42,"uid":0,"binary":"/bin/sh","arguments":"-c whoami"},"parent":{"pid":1,"uid":0,"binary":"/usr/sbin/sshd","arguments":"-D"}}}`

func testDetection(rule, ts string) string {
	event := map[string]any{
		"EventType":   "process_exec",
		"Image":       "/bin/sh",
		"CommandLine": "/bin/sh -c whoami",
		"User":        "root",
		"@timestamp":  ts,
	}
	det := map[string]any{
		"rule_title": rule,
		"rule_id":    "rule-" + rule,
		"level":      "high",
		"event":      event,
	}
	b, err := json.Marshal(det)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func TestSigmaSinkDeliversNormalizedEvents(t *testing.T) {
	dir := t.TempDir()
	sock := filepath.Join(dir, "rsigma.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	if err := ln.(*net.UnixListener).SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}

	s := newSigmaSink(sock, 8, t.Logf)
	defer s.close()
	s.offer(testSigmaEvent)
	s.offer(`{"process_kprobe":{"function_name":"security_file_open","process":{"binary":"/bin/cat"}}}`)
	s.offer(`not json`)

	conn, err := ln.Accept()
	if err != nil {
		t.Fatalf("accept: %v", err)
	}
	defer conn.Close()
	if err := conn.SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var ev sigma.Event
	if err := json.Unmarshal([]byte(line), &ev); err != nil {
		t.Fatalf("decode %q: %v", line, err)
	}
	if ev.EventType != "process_exec" || ev.Image != "/bin/sh" || ev.CommandLine != "/bin/sh -c whoami" {
		t.Fatalf("unexpected event: %+v", ev)
	}
	if ev.ParentImage != "/usr/sbin/sshd" || ev.User != "root" {
		t.Fatalf("missing parent/user: %+v", ev)
	}
	deadline := time.Now().Add(2 * time.Second)
	for s.sent.Load() != 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if s.sent.Load() != 1 {
		t.Fatalf("sent = %d, want 1", s.sent.Load())
	}
	if s.errors.Load() != 1 {
		t.Fatalf("errors = %d, want 1 for the malformed line", s.errors.Load())
	}
}

func TestSigmaSinkDoesNotBlockWhenEngineIsDown(t *testing.T) {
	s := newSigmaSink(filepath.Join(t.TempDir(), "missing.sock"), 1, func(string, ...any) {})
	defer s.close()

	start := time.Now()
	for i := 0; i < 5; i++ {
		s.offer(testSigmaEvent)
	}
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("offer blocked for %v", elapsed)
	}
	if s.dropped.Load() == 0 {
		t.Fatal("expected the bounded queue to drop events")
	}
	if s.sent.Load() != 0 {
		t.Fatalf("sent = %d without an engine", s.sent.Load())
	}
}

func TestSigmaDetectionToAlert(t *testing.T) {
	cfg := defaultConfig()
	cfg.destKeyFile = ""
	cfg.hostname = "node.test"
	line := testDetection("Linux Webshell Indicators", "2026-10-28T12:00:00Z")
	a, ok := sigmaDetectionToAlert(line, cfg)
	if !ok {
		t.Fatal("detection was not mapped")
	}
	if a.Labels["source"] != "sigma" || a.Labels["layer"] != "host" {
		t.Fatalf("labels = %+v", a.Labels)
	}
	if a.Labels["rule_id"] != "rule-Linux Webshell Indicators" {
		t.Fatalf("rule_id = %q", a.Labels["rule_id"])
	}
	if a.Labels["severity"] != "critical" {
		t.Fatalf("severity = %q", a.Labels["severity"])
	}
	if !strings.Contains(a.Annotations["summary"], "args=/bin/sh -c whoami") {
		t.Fatalf("summary = %q", a.Annotations["summary"])
	}
	if a.StartsAt != "2026-10-28T12:00:00Z" {
		t.Fatalf("StartsAt = %q", a.StartsAt)
	}
}

func TestSigmaRuleSlug(t *testing.T) {
	if got := sigmaRuleSlug("Linux  Webshell / Indicators!"); got != "linux-webshell-indicators" {
		t.Fatalf("slug = %q", got)
	}
	if got := sigmaRuleSlug("!!!"); got != "sigma" {
		t.Fatalf("empty slug = %q", got)
	}
}

func TestSigmaCursorRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "detections.cursor")
	if err := writeSigmaCursor(path, sigmaCursorState{Device: 1, Inode: 2, Offset: 345}); err != nil {
		t.Fatal(err)
	}
	state, ok, err := loadSigmaCursor(path)
	if err != nil || !ok {
		t.Fatalf("load = %v, %v", ok, err)
	}
	if state.Device != 1 || state.Inode != 2 || state.Offset != 345 {
		t.Fatalf("state = %+v", state)
	}
}

func TestSigmaDetectionsTailWithCursor(t *testing.T) {
	dir := t.TempDir()
	detPath := filepath.Join(dir, "detections.jsonl")
	// Timestamps must stay inside the 24h event-age cutoff, or the tail
	// handler drops them and the test fails with the passage of time.
	now := time.Now().UTC()
	first := testDetection("First", now.Add(-2*time.Minute).Format(time.RFC3339))
	if err := os.WriteFile(detPath, []byte(first+"\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	cfg := defaultConfig()
	cfg.destKeyFile = ""
	cfg.hostname = "node.test"
	cfg.detectionsFile = ""
	cfg.sigmaDetections = detPath
	cfg.sigmaCursorFile = filepath.Join(dir, "detections.cursor")
	cfg.pollInterval = 10 * time.Millisecond

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.startSigmaDetections(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	if depth := f.queue.depth(); depth != 0 {
		t.Fatalf("pre-existing detection was paged: depth=%d", depth)
	}

	second := testDetection("Second", now.Add(-time.Minute).Format(time.RFC3339))
	appendLine(t, detPath, second)
	waitFor(t, 3*time.Second, func() bool { return f.queue.depth() == 1 })
	a, _ := f.queue.pop()
	if a.Labels["rule_id"] != "rule-Second" {
		t.Fatalf("rule_id = %q", a.Labels["rule_id"])
	}
	waitFor(t, 2*time.Second, func() bool {
		state, ok, err := loadSigmaCursor(cfg.sigmaCursorFile)
		return err == nil && ok && state.Offset == int64(len(first)+1+len(second)+1)
	})

	f.shutdown()
	f.wg.Wait()

	// A restart resumes at the cursor: the two earlier detections stay
	// silent, and a new line is delivered.
	f2, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f2.startSigmaDetections(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		f2.shutdown()
		f2.wg.Wait()
	}()
	time.Sleep(100 * time.Millisecond)
	if depth := f2.queue.depth(); depth != 0 {
		t.Fatalf("cursor replay duplicated detections: depth=%d", depth)
	}
	appendLine(t, detPath, testDetection("Third", now.Format(time.RFC3339)))
	waitFor(t, 3*time.Second, func() bool { return f2.queue.depth() == 1 })
	a, _ = f2.queue.pop()
	if a.Labels["rule_id"] != "rule-Third" {
		t.Fatalf("rule_id after restart = %q", a.Labels["rule_id"])
	}
}

func appendLine(t *testing.T, path, line string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o640)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Fprintln(f, line); err != nil {
		f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}
