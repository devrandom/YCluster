package main

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestPostLoopDrainsSpool(t *testing.T) {
	var mu sync.Mutex
	got := 0
	srv := httptestAlertmanager(t, func([]byte) {
		mu.Lock()
		got++
		mu.Unlock()
	})
	cfg := testConfig()
	cfg.alertmanagerURL = srv
	cfg.pollInterval = 5 * time.Millisecond
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	cfg.spoolMaxBytes = 1 << 20
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", "r", "spooled")); err != nil {
		t.Fatal(err)
	}
	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return got == 1 && f.spool.depth() == 0
	})
	f.shutdown()
	f.wg.Wait()
	f.closeState()
}

func TestEnqueueReportsSpoolFailure(t *testing.T) {
	s, _ := testSpool(t, 1<<20)
	s.stop()
	cfg := testConfig()
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	f.spool = s
	if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", "r", "s")); err == nil {
		t.Fatal("enqueue must fail after spool stop")
	}
	if f.stats.dropped.Load() != 1 {
		t.Fatalf("dropped=%d", f.stats.dropped.Load())
	}
	s.close()
}

// A failed append must not mark the dedup key: the sensor or receiver
// will retry, and that retry has to reach the spool.
func TestFailedAppendDoesNotPoisonDedup(t *testing.T) {
	s, _ := testSpool(t, 1<<20)
	s.stop()
	cfg := testConfig()
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	f.spool = s
	a := newAlert(cfg, sourceTetragon, "warning", "host", "r", "retry me")
	if err := f.enqueue(a); err == nil {
		t.Fatal("expected a spool error")
	}
	if f.dedup.seen(a.dedupKey()) {
		t.Fatal("failed append must leave the dedup key unmarked")
	}
	s.close()
}

func TestEnqueueDropsOversizedWithMetric(t *testing.T) {
	cfg := testConfig()
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	huge := newAlert(cfg, sourceTetragon, "warning", "host", "huge", string(make([]byte, spoolMaxEntryBytes+1)))
	if err := f.enqueue(huge); err != nil {
		t.Fatalf("oversized alerts are dropped, not fatal: %v", err)
	}
	if f.stats.oversized.Load() != 1 || f.stats.dropped.Load() != 1 {
		t.Fatalf("oversized=%d dropped=%d", f.stats.oversized.Load(), f.stats.dropped.Load())
	}
	if f.spool.depth() != 0 {
		t.Fatalf("oversized alert reached the spool: depth=%d", f.spool.depth())
	}
}

func TestHandleLineReturnsSpoolError(t *testing.T) {
	cfg := testConfig()
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	cfg.cursorFile = filepath.Join(t.TempDir(), "cursor.json")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	f.spool.stop()
	if err := f.handleLine(tetragonLineAt(time.Now().UTC(), "boom")); err == nil {
		t.Fatal("handleLine must surface a spool failure")
	}
	if _, _, ok := f.cursor.boundary(); ok {
		t.Fatal("cursor must not advance past an unstored alert")
	}
}

func TestFailStopsAndRecordsError(t *testing.T) {
	f, err := newForwarder(testConfig())
	if err != nil {
		t.Fatal(err)
	}
	boom := errors.New("boom")
	f.fail(boom)
	f.fail(errors.New("later"))
	if !f.stopping() {
		t.Fatal("fail must stop the forwarder")
	}
	if f.failure() != boom {
		t.Fatalf("first fatal error must win, got %v", f.failure())
	}
	f.closeState()
}

func TestRunWithRejectsCursorWithoutSpool(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.cursorFile = filepath.Join(dir, "cursor.json")
	cfg.tetragonLog = ""
	cfg.metricsListen = ""
	if err := runWith(context.Background(), cfg); err == nil {
		t.Fatal("cursor without a durable spool must be rejected")
	}
}

func TestPostOneRejectsNon2xx(t *testing.T) {
	cfg := testConfig()
	cfg.alertmanagerURL = httptestAlertmanagerStatus(t, http.StatusInternalServerError)
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.post([]alert{newAlert(cfg, sourceTetragon, "warning", "host", "r", "s")}); err == nil {
		t.Fatal("500 response must fail the post")
	}
}

func TestReplaySkipsUnreadableAndNonLogFiles(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "tetragon.yaml"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(dir, "tetragon-bad.log.gz")
	if err := os.WriteFile(bad, []byte("not gzip"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, time.Now().Add(-time.Hour), tetragonLineAt(time.Now().Add(-2*time.Hour), "old"))
	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	if _, err := f.replayTetragonLogs(nil); err != nil {
		t.Fatal(err)
	}
	for _, a := range drainSpool(t, f) {
		if a.Labels["alertname"] != "IDSForwarderGap" {
			t.Fatalf("unexpected alert from unreadable files: %+v", a.Labels)
		}
	}
	files, err := tetragonLogFiles(current)
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		if filepath.Ext(file.path) == ".yaml" {
			t.Fatalf("non-log file included: %s", file.path)
		}
	}
}

func TestPostLoopRetriesSpoolOnAlertmanagerFailure(t *testing.T) {
	cfg := testConfig()
	cfg.alertmanagerURL = "http://127.0.0.1:1"
	cfg.pollInterval = 5 * time.Millisecond
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", "r", "retry")); err != nil {
		t.Fatal(err)
	}
	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 2*time.Second, func() bool { return f.stats.failures.Load() >= 1 })
	f.shutdown()
	f.wg.Wait()
	f.closeState()
	if f.stats.forwarded.Load() != 0 {
		t.Fatalf("unreachable Alertmanager must not forward: %d", f.stats.forwarded.Load())
	}
}

// Shutdown must not try to drain a large spool while Alertmanager is
// down; the unacked entries stay for the next start.
func TestPostLoopStopsDuringBacklog(t *testing.T) {
	cfg := testConfig()
	cfg.alertmanagerURL = "http://127.0.0.1:1"
	cfg.pollInterval = time.Second
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		summary := "backlog " + string(rune('a'+i))
		if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", "r", summary)); err != nil {
			t.Fatal(err)
		}
	}
	done := make(chan struct{})
	f.wg.Add(1)
	go func() {
		f.postLoop()
		close(done)
	}()
	waitFor(t, 6*time.Second, func() bool { return f.stats.failures.Load() >= 1 })
	start := time.Now()
	f.shutdown()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("post loop did not stop during a backlog")
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("shutdown waited for the retry interval: %s", elapsed)
	}
	if f.spool.depth() != 5 {
		t.Fatalf("shutdown must leave the backlog for the next start, depth=%d", f.spool.depth())
	}
	f.closeState()
}

func TestRunWithReportsStartFailure(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(logPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	cfg := testConfig()
	cfg.tetragonLog = logPath
	cfg.metricsListen = ln.Addr().String()
	if err := runWith(context.Background(), cfg); err == nil {
		t.Fatal("occupied metrics port must fail startup")
	}
}

func TestCursorSaveFailureIsNonFatal(t *testing.T) {
	dir := t.TempDir()
	c := newReplayCursor(dir, func(string, ...any) {})
	c.saveInterval = 0
	c.advance(time.Now().UTC(), "x")
	c.flush()
}

func TestReceiverReturns503WhenSpoolStopped(t *testing.T) {
	cfg := testConfig()
	cfg.httpListen = "127.0.0.1:0"
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.startReceiver(); err != nil {
		t.Fatal(err)
	}
	f.spool.stop()
	body := `[{"scenario":"bf","events":[{"message":"m"}]}]`
	resp, err := http.Post("http://"+f.receiverAddr()+"/crowdsec", "application/json",
		strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d", resp.StatusCode)
	}
	waitFor(t, 2*time.Second, func() bool { return f.failure() != nil })
	f.closeState()
}

func TestNewTailerFromFileRejectsClosedFileAndClampsOffset(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.log")
	if err := os.WriteFile(path, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	closed, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	closed.Close()
	if _, err := newTailerFromFile(closed, path, time.Millisecond, 0, func([]byte) {}); err == nil {
		t.Fatal("closed file must be rejected")
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	tl, err := newTailerFromFile(f, path, time.Millisecond, 999, func([]byte) {})
	if err != nil {
		t.Fatal(err)
	}
	tl.close()
}

func httptestAlertmanagerStatus(t *testing.T, code int) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(code)
		}),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { srv.Close() })
	return "http://" + ln.Addr().String()
}

// Corrupt and oversized records are consumed with counters instead of
// stopping the post loop.
func TestPostSpoolSkipsBadRecords(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "spool.jsonl")
	cfg := testConfig()
	good := newAlert(cfg, sourceTetragon, "warning", "host", "good", "keep")
	data, err := marshalSpoolAlert(good)
	if err != nil {
		t.Fatal(err)
	}
	huge := strings.Repeat("z", spoolMaxEntryBytes+2048)
	body := "{bad}\n" + `{"labels":{"rule_id":"huge"},"annotations":{"summary":"` + huge + `"}}` + "\n" + string(data) + "\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	got := 0
	srv := httptestAlertmanager(t, func([]byte) {
		mu.Lock()
		got++
		mu.Unlock()
	})
	cfg.alertmanagerURL = srv
	cfg.pollInterval = 5 * time.Millisecond
	cfg.spoolFile = path
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return got == 1 && f.spool.depth() == 0
	})
	f.shutdown()
	f.wg.Wait()
	f.closeState()
	if f.stats.oversized.Load() != 1 || f.stats.dropped.Load() != 2 {
		t.Fatalf("oversized=%d dropped=%d", f.stats.oversized.Load(), f.stats.dropped.Load())
	}
}

// If the ack cannot be written, delivery progress is no longer durable:
// the process must fail so systemd restarts it and replays the alert.
func TestPostSpoolAckFailureIsFatal(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses directory permissions")
	}
	dir := t.TempDir()
	cfg := testConfig()
	cfg.alertmanagerURL = httptestAlertmanager(t, func([]byte) {})
	cfg.pollInterval = 5 * time.Millisecond
	cfg.spoolFile = filepath.Join(dir, "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", "r", "ack")); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 2*time.Second, func() bool { return f.failure() != nil })
	f.shutdown()
	f.wg.Wait()
	f.closeState()
	if f.stats.forwarded.Load() != 1 {
		t.Fatalf("forwarded=%d", f.stats.forwarded.Load())
	}
}

// A payload Alertmanager rejects with 4xx must not block the queue.
func TestPostSpoolDropsPermanentlyRejectedAlerts(t *testing.T) {
	cfg := testConfig()
	cfg.alertmanagerURL = httptestAlertmanagerStatus(t, http.StatusBadRequest)
	cfg.pollInterval = 5 * time.Millisecond
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, rule := range []string{"r1", "r2"} {
		if err := f.enqueue(newAlert(cfg, sourceTetragon, "warning", "host", rule, rule)); err != nil {
			t.Fatal(err)
		}
	}
	f.wg.Add(1)
	go f.postLoop()
	waitFor(t, 2*time.Second, func() bool {
		return f.stats.rejected.Load() == 2 && f.spool.depth() == 0
	})
	f.shutdown()
	f.wg.Wait()
	f.closeState()
	if f.stats.forwarded.Load() != 0 {
		t.Fatalf("rejected alerts must not count as forwarded: %d", f.stats.forwarded.Load())
	}
}

// 429 and 5xx stay retryable.
func TestPostOneKeepsRetryableStatuses(t *testing.T) {
	for _, code := range []int{http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		cfg := testConfig()
		cfg.alertmanagerURL = httptestAlertmanagerStatus(t, code)
		f, err := newForwarder(cfg)
		if err != nil {
			t.Fatal(err)
		}
		err = f.post([]alert{newAlert(cfg, sourceTetragon, "warning", "host", "r", "s")})
		var perm *permanentPostError
		if err == nil || errors.As(err, &perm) {
			t.Fatalf("status %d must be a transient failure, got %v", code, err)
		}
	}
}

func TestCursorSaveFailureIncrementsMetric(t *testing.T) {
	cfg := testConfig()
	cfg.cursorFile = t.TempDir()
	cfg.spoolFile = filepath.Join(t.TempDir(), "spool.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	f.cursor.saveInterval = 0
	f.cursor.advance(time.Now().UTC(), "h")
	if f.stats.cursorErrors.Load() == 0 {
		t.Fatal("failed cursor save must be exported")
	}
}

// If the gap alert itself cannot be spooled, the failure must surface
// instead of being logged after the gap latch is set.
func TestGapAlertSpoolFailurePropagates(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "tetragon.log")
	if err := os.WriteFile(current, []byte(tetragonLineAt(time.Now().UTC(), "x")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cursorPath := filepath.Join(dir, "cursor.json")
	seedCursor(t, cursorPath, time.Now().Add(-time.Hour), tetragonLineAt(time.Now().Add(-2*time.Hour), "old"))
	f, err := newForwarder(replayConfig(t, current, cursorPath))
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()
	f.spool.stop()
	if _, err := f.replayTetragonLogs(nil); err == nil {
		t.Fatal("a gap alert that cannot be spooled must fail replay")
	}
	if f.stats.gaps.Load() != 0 {
		t.Fatalf("gap counter must not claim a durable gap: %d", f.stats.gaps.Load())
	}
}
