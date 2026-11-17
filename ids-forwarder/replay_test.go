package main

import (
	"compress/gzip"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func tetragonLineAt(t time.Time, rule string) string {
	return fmt.Sprintf(
		`{"process_kprobe":{"policy_name":%q,"message":"test","process":{"binary":"/bin/sh"}},"time":%q}`,
		rule, t.UTC().Format(time.RFC3339Nano))
}

// pagingLineAt is a privilege raise on the host by a non-root identity: a
// class that pages, for tests that need an alert to reach the delivery path.
// The namespace snapshot is what makes the layer host rather than unverified.
func pagingLineAt(t time.Time, rule string) string {
	return fmt.Sprintf(
		`{"process_kprobe":{"policy_name":"privilege-raises","function_name":"__sys_setuid","message":%q,"process":{"pid":900,"uid":1000,"binary":"/usr/bin/sudo","ns":{"pid":{"inum":4026531836,"is_host":true},"mnt":{"inum":4026531841,"is_host":true}}},"args":[{"int_arg":0}]},"time":%q}`,
		rule, t.UTC().Format(time.RFC3339Nano))
}

func writeGzipLog(t *testing.T, path string, lines ...string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	gz := gzip.NewWriter(f)
	for _, line := range lines {
		if _, err := gz.Write([]byte(line + "\n")); err != nil {
			t.Fatal(err)
		}
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}

func replayConfig(t *testing.T, logPath, cursorPath string) config {
	t.Helper()
	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.cursorFile = cursorPath
	cfg.spoolFile = filepath.Join(filepath.Dir(cursorPath), "spool.jsonl")
	cfg.detectionsFile = filepath.Join(filepath.Dir(cursorPath), "detections.jsonl")
	cfg.spoolMaxBytes = 1 << 20
	cfg.queueMax = 100
	cfg.events = "process_kprobe"
	return cfg
}

// processedRules returns the rule ids the forwarder handled, in order, from
// the detection record. A detection that cannot page stops at the record by
// design, so the record — not the spool — is the complete list; the spool
// holds only what will be delivered.
func processedRules(t *testing.T, f *forwarder) []string {
	t.Helper()
	data, err := os.ReadFile(f.cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, line := range splitLines(string(data)) {
		if line == "" {
			continue
		}
		var rec detectionRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatal(err)
		}
		out = append(out, rec.RuleID)
	}
	return out
}

func seedCursor(t *testing.T, path string, at time.Time, line string) {
	t.Helper()
	c := newReplayCursor(path, func(string, ...any) {})
	c.saveInterval = 0
	c.advance(at, lineHash(line))
	c.flush()
}

// recordedCounter reports how many times a rule id is in the detection
// record. A detection that cannot page never reaches Alertmanager, so the
// record is what tells us the forwarder processed it.
func recordedCounter(path string) func(string) int {
	return func(rule string) int {
		data, err := os.ReadFile(path)
		if err != nil {
			return 0
		}
		n := 0
		for _, line := range splitLines(string(data)) {
			if line == "" {
				continue
			}
			var rec detectionRecord
			if json.Unmarshal([]byte(line), &rec) == nil && rec.RuleID == rule {
				n++
			}
		}
		return n
	}
}

func drainSpool(t *testing.T, f *forwarder) []alert {
	t.Helper()
	var out []alert
	for {
		item, ok, err := f.spool.next()
		if err != nil {
			t.Fatal(err)
		}
		if !ok {
			return out
		}
		if err := f.spool.ackTo(item.token); err != nil {
			t.Fatal(err)
		}
		out = append(out, item.alert)
	}
}

func TestReplaySkipsCompressedBoundaryAndQueuesNewEvents(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	old := now.Add(-time.Hour)
	boundary := old.Add(time.Minute)
	newer := old.Add(2 * time.Minute)
	line1 := tetragonLineAt(old, "old")
	line2 := tetragonLineAt(boundary, "boundary")
	line3 := tetragonLineAt(newer, "new")

	rotated := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, rotated, line1, line2)
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(line3+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, boundary, line2)

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	got := processedRules(t, f)
	if len(got) != 1 || got[0] != "new" {
		t.Fatalf("expected only the event after the cursor, got %v", got)
	}
}

func TestReplayEmitsGapWhenRetentionOvershootsCursor(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	old := now.Add(-2 * time.Hour)
	line := tetragonLineAt(now.Add(-time.Minute), "new")
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(line+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, old, tetragonLineAt(old, "old"))

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	if f.stats.gaps.Load() != 1 {
		t.Fatalf("gap counter=%d", f.stats.gaps.Load())
	}
	var sawGap, sawEvent bool
	for _, rule := range processedRules(t, f) {
		if rule == "forwarder-gap" {
			sawGap = true
		}
		if rule == "new" {
			sawEvent = true
		}
	}
	if !sawGap || !sawEvent {
		t.Fatalf("gap=%v event=%v", sawGap, sawEvent)
	}
}

// Events that share the boundary timestamp but follow it in the file are
// new and must not be swallowed along with the boundary.
func TestReplayKeepsSameTimestampAfterBoundary(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	boundary := tetragonLineAt(now, "boundary")
	next := tetragonLineAt(now, "next")
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(boundary+"\n"+next+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now, boundary)

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	got := processedRules(t, f)
	if len(got) != 1 || got[0] != "next" {
		t.Fatalf("same-timestamp event after the boundary was lost: %v", got)
	}
}

// A timestamp watermark would drop "late" here simply because its clock
// reading is older than an already-processed event.
func TestReplayKeepsOutOfOrderTimestamps(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	first := tetragonLineAt(now.Add(-time.Minute), "first")
	late := tetragonLineAt(now.Add(-30*time.Second), "late")
	newest := tetragonLineAt(now.Add(-10*time.Second), "newest")
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(first+"\n"+newest+"\n"+late+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now.Add(-time.Minute), first)

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	rules := processedRules(t, f)
	if strings.Join(rules, ",") != "newest,late" {
		t.Fatalf("out-of-order event lost: %v", rules)
	}
	if f.stats.gaps.Load() != 0 {
		t.Fatalf("no gap expected, got %d", f.stats.gaps.Load())
	}
}

// Once the boundary file is rotated out, everything retained is newer
// than the cursor and must be replayed behind a gap alert.
func TestReplayGapReplaysEverythingRetained(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	rotated := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, rotated, tetragonLineAt(now.Add(-2*time.Minute), "a"))
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(tetragonLineAt(now.Add(-time.Minute), "b")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now.Add(-time.Hour), tetragonLineAt(now.Add(-time.Hour), "gone"))

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	rules := processedRules(t, f)
	if strings.Join(rules, ",") != "forwarder-gap,a,b" {
		t.Fatalf("gap replay = %v", rules)
	}
	// The gap is a coverage alert, not a detection finding: it is still
	// delivered, while the events around it are only recorded.
	if delivered := drainSpool(t, f); len(delivered) != 1 || delivered[0].Labels["rule_id"] != "forwarder-gap" {
		t.Fatalf("gap delivery = %+v", delivered)
	}
}

// A cursor boundary that is unaffected by file mtimes must still be
// found when the backup's mtime is older than the cursor timestamp.
func TestReplayIgnoresMtimeFilter(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	line1 := tetragonLineAt(now.Add(-time.Hour), "old")
	line2 := tetragonLineAt(now.Add(-time.Hour).Add(time.Second), "new")
	rotated := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, rotated, line1, line2)
	if err := os.Chtimes(rotated, now.Add(-48*time.Hour), now.Add(-48*time.Hour)); err != nil {
		t.Fatal(err)
	}
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now.Add(-time.Hour), line1)

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	got := processedRules(t, f)
	if len(got) != 1 || got[0] != "new" {
		t.Fatalf("replay skipped a file by mtime: %v", got)
	}
}

func TestForwarderStartupGraceRecoversRecentEvent(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	line := tetragonLineAt(time.Now().UTC().Add(-30*time.Second), "recent")
	if err := os.WriteFile(logPath, []byte(line+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.cursorFile = filepath.Join(dir, "cursor.json")
	cfg.spoolFile = filepath.Join(dir, "spool.jsonl")
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")
	cfg.pollInterval = 5 * time.Millisecond
	cfg.metricsListen = ""
	cfg.httpListen = ""
	cfg.dedupWindow = 0
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.start(); err != nil {
		t.Fatal(err)
	}
	seen := recordedCounter(cfg.detectionsFile)
	waitFor(t, 2*time.Second, func() bool { return seen("recent") == 1 })
	f.shutdown()
	f.wg.Wait()
	f.closeState()
}

func TestForwarderRestartReplaysRotatedEvents(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	cursorPath := filepath.Join(dir, "cursor.json")
	spoolPath := filepath.Join(dir, "spool.jsonl")

	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.cursorFile = cursorPath
	cfg.spoolFile = spoolPath
	cfg.spoolMaxBytes = 1 << 20
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")
	cfg.pollInterval = 5 * time.Millisecond
	cfg.metricsListen = ""
	cfg.httpListen = ""
	cfg.dedupWindow = 0
	cfg.startupGrace = 0
	cfg.events = "process_kprobe"

	countRule := recordedCounter(cfg.detectionsFile)

	now := time.Now().UTC()
	line1 := tetragonLineAt(now.Add(-3*time.Minute), "one")
	if err := os.WriteFile(logPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	f1, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f1.start(); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(line1 + "\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	waitFor(t, 3*time.Second, func() bool { return countRule("one") == 1 })
	f1.shutdown()
	f1.wg.Wait()
	f1.closeState()

	// While the forwarder is down, event two lands in the current file
	// and then that file is rotated/compressed. Event three goes to the
	// new current file.
	line2 := tetragonLineAt(now.Add(-2*time.Minute), "two")
	line3 := tetragonLineAt(now.Add(-time.Minute), "three")
	f, err = os.OpenFile(logPath, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(line2 + "\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	content, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(logPath); err != nil {
		t.Fatal(err)
	}
	rotated := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, rotated, strings.Split(strings.TrimSpace(string(content)), "\n")...)
	if err := os.WriteFile(logPath, []byte(line3+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	f2, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f2.start(); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 3*time.Second, func() bool {
		return countRule("two") == 1 && countRule("three") == 1
	})
	f2.shutdown()
	f2.wg.Wait()
	f2.closeState()

	if got := countRule("one"); got != 1 {
		t.Fatalf("restart replayed the already-forwarded event: got %d", got)
	}
}

func TestTailerFromHeldFileReadsOldInodeBeforeRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.log")
	if err := os.WriteFile(path, []byte("old1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	// Tetragon appends after the forwarder has taken the descriptor.
	af, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := af.WriteString("old2\n"); err != nil {
		t.Fatal(err)
	}
	af.Close()
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var got []string
	tl, err := newTailerFromFile(f, path, 5*time.Millisecond, 0, func(line []byte) {
		mu.Lock()
		got = append(got, string(line))
		mu.Unlock()
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 3
	})
	mu.Lock()
	defer mu.Unlock()
	if strings.Join(got, ",") != "old1,old2,new" {
		t.Fatalf("held-file handoff lost or reordered lines: %v", got)
	}
}

func TestTailerAtEndSkipsPreexistingLines(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.log")
	if err := os.WriteFile(path, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var got []string
	tl, err := newTailerAtEnd(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		got = append(got, string(line))
		mu.Unlock()
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("new\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	mu.Lock()
	defer mu.Unlock()
	if got[0] != "new" {
		t.Fatalf("tailer replayed the pre-existing line: %v", got)
	}
}

func TestTailerRotationInvokesReplayHook(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.log")
	if err := os.WriteFile(path, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var got []string
	tl, err := newTailer(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		got = append(got, string(line))
		mu.Unlock()
	})
	if err != nil {
		t.Fatal(err)
	}
	rotated := make(chan struct{}, 1)
	tl.onRotate = func() { rotated <- struct{}{} }
	go tl.run()
	defer tl.close()
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	select {
	case <-rotated:
	case <-time.After(2 * time.Second):
		t.Fatal("rotation hook not called")
	}
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 2
	})
}

// A restart with the boundary at the end of the live file must not
// re-deliver the prefix, and must still pick up lines appended after it.
func TestForwarderRestartSkipsReplayedPrefix(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	cursorPath := filepath.Join(dir, "cursor.json")
	now := time.Now().UTC()
	old := tetragonLineAt(now.Add(-3*time.Minute), "old")
	boundary := tetragonLineAt(now.Add(-2*time.Minute), "boundary")
	fresh := tetragonLineAt(now.Add(-time.Minute), "fresh")
	if err := os.WriteFile(logPath, []byte(old+"\n"+boundary+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	seedCursor(t, cursorPath, now.Add(-2*time.Minute), boundary)

	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.cursorFile = cursorPath
	cfg.spoolFile = filepath.Join(dir, "spool.jsonl")
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")
	cfg.pollInterval = 5 * time.Millisecond
	cfg.metricsListen = ""
	cfg.httpListen = ""
	cfg.dedupWindow = 0
	cfg.events = "process_kprobe"
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		f.shutdown()
		f.wg.Wait()
		f.closeState()
	}()
	af, err := os.OpenFile(logPath, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := af.WriteString(fresh + "\n"); err != nil {
		t.Fatal(err)
	}
	af.Close()
	seen := recordedCounter(cfg.detectionsFile)
	waitFor(t, 2*time.Second, func() bool { return seen("fresh") == 1 })
	if got := seen("old"); got != 0 {
		t.Fatalf("replayed prefix leaked through the gate: %d old records", got)
	}
}

// A corrupt cursor means the position is unknown: replay what is
// retained and make the uncertainty visible.
func TestReplayCorruptCursorReplaysAndReportsGap(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(tetragonLineAt(now, "retained")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	if err := os.WriteFile(cursorPath, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	rules := processedRules(t, f)
	if strings.Join(rules, ",") != "forwarder-gap,retained" {
		t.Fatalf("corrupt cursor replay = %v", rules)
	}
}

// The startup grace is a floor for a first run, not a cursor: older
// retained events are ignored without claiming a coverage gap.
func TestReplayFirstRunFloorSkipsOldEvents(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	current := filepath.Join(dir, "tetragon.log")
	body := tetragonLineAt(now.Add(-time.Hour), "old") + "\n" +
		tetragonLineAt(now.Add(-time.Second), "recent") + "\n"
	if err := os.WriteFile(current, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := replayConfig(t, current, filepath.Join(dir, "cursor.json"))
	cfg.startupGrace = time.Minute
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	got := processedRules(t, f)
	if len(got) != 1 || got[0] != "recent" {
		t.Fatalf("startup floor replay = %v", got)
	}
	if f.stats.gaps.Load() != 0 {
		t.Fatalf("first run must not report a gap, got %d", f.stats.gaps.Load())
	}
}

// The live file can rotate between opening it and replaying. Replay must
// still scan the held inode (including bytes appended after the open) and
// hand the tailer an offset into that same inode.
func TestReplayHandoffScansHeldInodeAcrossRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "tetragon.log")
	now := time.Now().UTC()
	line1 := tetragonLineAt(now.Add(-3*time.Minute), "one")
	line1b := tetragonLineAt(now.Add(-2*time.Minute), "one-b")
	line2 := tetragonLineAt(now.Add(-time.Minute), "two")
	if err := os.WriteFile(path, []byte(line1+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	held, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	af, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := af.WriteString(line1b + "\n"); err != nil {
		t.Fatal(err)
	}
	af.Close()
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(line2+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now.Add(-3*time.Minute), line1)

	f, err := newForwarder(replayConfig(t, path, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	offset, err := f.replayTetragonLogs(held)
	if err != nil {
		t.Fatal(err)
	}
	want := int64(len(line1) + 1 + len(line1b) + 1)
	if offset != want {
		t.Fatalf("handoff offset = %d, want %d", offset, want)
	}
	rules := processedRules(t, f)
	if strings.Join(rules, ",") != "one-b,two" {
		t.Fatalf("rotation during replay lost or duplicated events: %v", rules)
	}
}

// A corrupt retained file after the boundary is a coverage loss: replay
// must say so rather than silently treating the file as empty.
func TestReplayReportsUnreadableFileAfterBoundary(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	line1 := tetragonLineAt(now.Add(-3*time.Minute), "boundary")
	line3 := tetragonLineAt(now.Add(-time.Minute), "current")
	first := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, first, line1)
	if err := os.Chtimes(first, now.Add(-3*time.Hour), now.Add(-3*time.Hour)); err != nil {
		t.Fatal(err)
	}
	middle := filepath.Join(dir, "tetragon-20260101T010000.log.gz")
	if err := os.WriteFile(middle, []byte("not gzip"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(middle, now.Add(-2*time.Hour), now.Add(-2*time.Hour)); err != nil {
		t.Fatal(err)
	}
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(line3+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, now.Add(-3*time.Minute), line1)

	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	rules := processedRules(t, f)
	got := strings.Join(rules, ",")
	if !strings.Contains(got, "forwarder-gap") || !strings.Contains(got, "current") {
		t.Fatalf("unreadable file must produce a gap plus later events: %v", rules)
	}
	if delivered := drainSpool(t, f); len(delivered) != 1 || delivered[0].Labels["rule_id"] != "forwarder-gap" {
		t.Fatalf("the coverage gap must still be delivered: %+v", delivered)
	}
}

// Tetragon renames a rotated file from "<name>.log" to "<name>.log.gz".
// A listing taken just before the rename must not turn into a gap alert.
func TestReplayFallsBackToCompressedName(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	gz := filepath.Join(dir, "tetragon-20260101T000000.log.gz")
	writeGzipLog(t, gz, tetragonLineAt(now, "compressed"))
	stem := strings.TrimSuffix(gz, ".gz")
	dev, ino := fileIdentity(mustStat(t, gz))
	src := replaySource{path: stem, dev: dev, ino: ino}

	var rules []string
	if _, err := scanSource(src, nil, func(line string) error {
		_, env, _ := eventType(line)
		if env != nil {
			rules = append(rules, string(env.Kprobe.PolicyName))
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(rules) != 1 || rules[0] != "compressed" {
		t.Fatalf("compressed fallback did not read the rotated file: %v", rules)
	}
}

// A file that simply vanished (compressed away or aged out) must not be
// reported as unreadable.
func TestReplaySkipsVanishedFile(t *testing.T) {
	src := replaySource{path: filepath.Join(t.TempDir(), "gone.log")}
	n, err := scanSource(src, nil, func(string) error { return nil })
	if err != nil {
		t.Fatalf("vanished file must not be an error: %v", err)
	}
	if n != 0 {
		t.Fatalf("vanished file consumed %d bytes", n)
	}
}

func mustStat(t *testing.T, path string) os.FileInfo {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info
}
