package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func marshalSpoolAlert(a alert) ([]byte, error) {
	return json.Marshal(a)
}

func testSpool(t *testing.T, max int64) (*alertSpool, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	s, err := openAlertSpool(path, max, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	return s, path
}

func TestSpoolAppendAckAndRecover(t *testing.T) {
	s, path := testSpool(t, 1<<20)
	cfg := testConfig()
	a1 := newAlert(cfg, sourceTetragon, "warning", "host", "r1", "one")
	a2 := newAlert(cfg, sourceTetragon, "warning", "host", "r2", "two")
	if err := s.append(a1); err != nil {
		t.Fatal(err)
	}
	if err := s.append(a2); err != nil {
		t.Fatal(err)
	}
	if s.depth() != 2 || s.pendingBytes() == 0 {
		t.Fatalf("depth=%d bytes=%d", s.depth(), s.pendingBytes())
	}
	item, ok, err := s.next()
	if err != nil || !ok {
		t.Fatalf("next: ok=%v err=%v", ok, err)
	}
	if item.alert.Labels["rule_id"] != "r1" {
		t.Fatalf("wrong first alert: %v", item.alert.Labels)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	if s.depth() != 1 {
		t.Fatalf("depth after ack=%d", s.depth())
	}
	s.close()

	s, err = openAlertSpool(path, 1<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if s.depth() != 1 {
		t.Fatalf("recovered depth=%d", s.depth())
	}
	item, ok, err = s.next()
	if err != nil || !ok {
		t.Fatalf("recovered next: ok=%v err=%v", ok, err)
	}
	if item.alert.Labels["rule_id"] != "r2" {
		t.Fatalf("wrong recovered alert: %v", item.alert.Labels)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	if s.depth() != 0 || s.pendingBytes() != 0 {
		t.Fatalf("spool did not empty: depth=%d bytes=%d", s.depth(), s.pendingBytes())
	}
}

func TestSpoolTrimsTornLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "r", "one")
	data, err := marshalSpoolAlert(a)
	if err != nil {
		t.Fatal(err)
	}
	body := append(append(data, '\n'), []byte("{\"labels\":")...)
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := openAlertSpool(path, 1<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if s.depth() != 1 {
		t.Fatalf("expected one complete entry, depth=%d", s.depth())
	}
	if _, ok, err := s.next(); err != nil || !ok {
		t.Fatalf("next: ok=%v err=%v", ok, err)
	}
}

func TestSpoolBackpressureUnblocksOnStop(t *testing.T) {
	s, _ := testSpool(t, 10)
	cfg := testConfig()
	done := make(chan error, 1)
	go func() {
		done <- s.append(newAlert(cfg, sourceTetragon, "warning", "host", "r", "too large"))
	}()
	select {
	case err := <-done:
		t.Fatalf("append should block on capacity, returned %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	s.stop()
	select {
	case err := <-done:
		if !errors.Is(err, errSpoolClosed) {
			t.Fatalf("expected errSpoolClosed, got %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("append did not unblock after stop")
	}
	s.close()
}

func TestSpoolCorruptEntryIsSkipped(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	if err := os.WriteFile(path, []byte("{bad}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := openAlertSpool(path, 1<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if _, ok, err := s.next(); !errors.Is(err, errSpoolCorrupt) || ok {
		t.Fatalf("expected corrupt entry error, ok=%v err=%v", ok, err)
	}
	if _, ok, err := s.next(); err != nil || ok {
		t.Fatalf("corrupt entry must be consumed, ok=%v err=%v", ok, err)
	}
}

func TestSpoolOversizedEntryIsConsumed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	huge := strings.Repeat("x", spoolMaxEntryBytes+4096)
	body := `{"labels":{"rule_id":"huge"},"annotations":{"summary":"` + huge + `"}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := openAlertSpool(path, 100<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if _, ok, err := s.next(); !errors.Is(err, errSpoolEntryTooLarge) || ok {
		t.Fatalf("expected oversized error, ok=%v err=%v", ok, err)
	}
	if _, ok, err := s.next(); err != nil || ok {
		t.Fatalf("oversized entry must be consumed, ok=%v err=%v", ok, err)
	}
	if s.depth() != 0 {
		t.Fatalf("depth=%d", s.depth())
	}
}

func TestSpoolAppendRejectsOversizedAlert(t *testing.T) {
	s, _ := testSpool(t, 100<<20)
	defer s.close()
	cfg := testConfig()
	huge := newAlert(cfg, sourceTetragon, "warning", "host", "huge", "bounded summary")
	huge.Labels["padding"] = strings.Repeat("x", spoolMaxEntryBytes+1)
	if err := s.append(huge); !errors.Is(err, errSpoolEntryTooLarge) {
		t.Fatalf("expected size rejection, got %v", err)
	}
	if s.depth() != 0 {
		t.Fatalf("rejected entry must not be queued, depth=%d", s.depth())
	}
}

func TestSpoolCompactsAfterAck(t *testing.T) {
	s, _ := testSpool(t, 10<<20)
	cfg := testConfig()
	big := newAlert(cfg, sourceTetragon, "warning", "host", "big", "summary")
	big.Labels["padding"] = strings.Repeat("x", 2<<20)
	small := newAlert(cfg, sourceTetragon, "warning", "host", "small", "tail")
	if err := s.append(big); err != nil {
		t.Fatal(err)
	}
	if err := s.append(small); err != nil {
		t.Fatal(err)
	}
	item, ok, err := s.next()
	if err != nil || !ok {
		t.Fatalf("next: ok=%v err=%v", ok, err)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	if s.depth() != 1 || s.pendingBytes() >= int64(len(big.Labels["padding"])) {
		t.Fatalf("compaction did not shrink spool: depth=%d bytes=%d", s.depth(), s.pendingBytes())
	}
	item, ok, err = s.next()
	if err != nil || !ok || item.alert.Labels["rule_id"] != "small" {
		t.Fatalf("surviving entry wrong: %+v ok=%v err=%v", item.alert.Labels, ok, err)
	}
	s.close()
}

// A crash after the compacted file is in place but before the new ack is
// durable leaves the old (larger) offset behind. Recovery must replay the
// surviving tail, never treat the stale offset as consumed.
func TestSpoolRecoveryAfterCompactionCrashWindow(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	cfg := testConfig()
	big := newAlert(cfg, sourceTetragon, "warning", "host", "big", "summary")
	big.Labels["padding"] = strings.Repeat("y", 2<<20)
	small := newAlert(cfg, sourceTetragon, "warning", "host", "small", "tail")

	s, err := openAlertSpool(path, 10<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.append(big); err != nil {
		t.Fatal(err)
	}
	if err := s.append(small); err != nil {
		t.Fatal(err)
	}
	item, ok, err := s.next()
	if err != nil || !ok {
		t.Fatalf("next: ok=%v err=%v", ok, err)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	stale := item.token
	s.close()

	// Simulate the window: the data file is the compacted tail, the ack
	// still points past its end.
	ack := spoolAck{Offset: stale}
	data, err := json.Marshal(ack)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path+".ack", data, 0o600); err != nil {
		t.Fatal(err)
	}

	s, err = openAlertSpool(path, 10<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if s.depth() != 1 {
		t.Fatalf("stale ack lost the unacked tail: depth=%d", s.depth())
	}
	item, ok, err = s.next()
	if err != nil || !ok || item.alert.Labels["rule_id"] != "small" {
		t.Fatalf("wrong surviving alert: %+v ok=%v err=%v", item.alert.Labels, ok, err)
	}
}

// An ack offset that still fits but no longer matches the bytes before it
// means the data file was rewritten or truncated at the same size.
func TestSpoolAckAnchorMismatchReplays(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "first", "one")
	b := newAlert(cfg, sourceTetragon, "warning", "host", "second", "two")
	s, err := openAlertSpool(path, 1<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.append(a); err != nil {
		t.Fatal(err)
	}
	if err := s.append(b); err != nil {
		t.Fatal(err)
	}
	item, _, err := s.next()
	if err != nil {
		t.Fatal(err)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	s.close()

	ackData, err := os.ReadFile(path + ".ack")
	if err != nil {
		t.Fatal(err)
	}
	var ack spoolAck
	if err := json.Unmarshal(ackData, &ack); err != nil {
		t.Fatal(err)
	}
	if ack.Anchor == "" {
		t.Fatal("expected an anchor in the ack")
	}
	ack.Anchor = "deadbeef"
	ackData, err = json.Marshal(ack)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path+".ack", ackData, 0o600); err != nil {
		t.Fatal(err)
	}

	s, err = openAlertSpool(path, 1<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if s.depth() != 2 {
		t.Fatalf("mismatched ack must replay both alerts, depth=%d", s.depth())
	}
}

func TestOpenSpoolRejectsDirectory(t *testing.T) {
	dir := t.TempDir()
	if _, err := openAlertSpool(dir, 1<<20, func(string, ...any) {}); err == nil {
		t.Fatal("directory path must fail")
	}
	cfg := testConfig()
	cfg.spoolFile = dir
	if _, err := newForwarder(cfg); err == nil {
		t.Fatal("newForwarder must surface a spool open failure")
	}
}

func TestSpoolRecoverCompactsAckedPrefix(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spool.jsonl")
	cfg := testConfig()
	big := newAlert(cfg, sourceTetragon, "warning", "host", "big", strings.Repeat("y", 2<<20))
	small := newAlert(cfg, sourceTetragon, "warning", "host", "small", "tail")
	bigData, err := marshalSpoolAlert(big)
	if err != nil {
		t.Fatal(err)
	}
	smallData, err := marshalSpoolAlert(small)
	if err != nil {
		t.Fatal(err)
	}
	body := append(append(append(bigData, '\n'), smallData...), '\n')
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	s, err := openAlertSpool(path, 10<<20, func(string, ...any) {})
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if s.depth() != 2 {
		t.Fatalf("depth=%d", s.depth())
	}
	item, ok, err := s.next()
	if err != nil || !ok {
		t.Fatalf("next: ok=%v err=%v", ok, err)
	}
	if err := s.ackTo(item.token); err != nil {
		t.Fatal(err)
	}
	if s.depth() != 1 {
		t.Fatalf("depth after ack=%d", s.depth())
	}
	item, ok, err = s.next()
	if err != nil || !ok || item.alert.Labels["rule_id"] != "small" {
		t.Fatalf("wrong recovered tail: %+v ok=%v err=%v", item.alert.Labels, ok, err)
	}
}
