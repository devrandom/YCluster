package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestReplayCursorRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cursor.json")
	c := newReplayCursor(path, func(string, ...any) {})
	c.saveInterval = 0
	now := time.Now().UTC().Truncate(time.Nanosecond)
	older := now.Add(-time.Minute)
	// The cursor tracks the last line in scan order, not a high-water
	// mark, so a later line with an older timestamp must win.
	c.advance(now, "a")
	c.advance(older, "b")
	c.flush()

	loaded := newReplayCursor(path, func(string, ...any) {})
	if !loaded.load() || !loaded.hasState() {
		t.Fatal("cursor state did not reload")
	}
	last, hash, ok := loaded.boundary()
	if !ok || hash != "b" || !last.Equal(older) {
		t.Fatalf("boundary = %v %q %v, want %v b", last, hash, ok, older)
	}
}

func TestReplayCursorMissingFileIsFirstStart(t *testing.T) {
	c := newReplayCursor(filepath.Join(t.TempDir(), "cursor.json"), func(string, ...any) {})
	if c.load() {
		t.Fatal("missing cursor must not load")
	}
	if c.hasState() {
		t.Fatal("missing cursor must not look like a lost position")
	}
	if _, _, ok := c.boundary(); ok {
		t.Fatal("missing cursor has no boundary")
	}
}

func TestReplayCursorCorruptStateIsUnknown(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cursor.json")
	if err := os.WriteFile(path, []byte("{bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := newReplayCursor(path, func(string, ...any) {})
	if c.load() {
		t.Fatal("corrupt cursor must not load as valid")
	}
	if !c.hasState() {
		t.Fatal("corrupt cursor must force a replay")
	}
	if _, _, ok := c.boundary(); ok {
		t.Fatal("corrupt cursor has no boundary")
	}
}

func TestReplayCursorReadErrorIsUnknown(t *testing.T) {
	dir := t.TempDir()
	c := newReplayCursor(dir, func(string, ...any) {})
	if c.load() {
		t.Fatal("directory cursor must not load")
	}
	if !c.hasState() {
		t.Fatal("read error must force a replay")
	}
}

func TestReplayCursorIgnoresEmptyHash(t *testing.T) {
	c := newReplayCursor(filepath.Join(t.TempDir(), "cursor.json"), func(string, ...any) {})
	c.advance(time.Now(), "kept")
	c.advance(time.Now().Add(time.Hour), "")
	_, hash, ok := c.boundary()
	if !ok || hash != "kept" {
		t.Fatalf("empty hash must not move the boundary: %q %v", hash, ok)
	}
}
