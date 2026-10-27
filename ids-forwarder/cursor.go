package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// replayCursor records the exact last Tetragon line processed, in scan
// order. It is deliberately not a time watermark: Tetragon can write
// events whose timestamps are older than events already processed, and a
// watermark would silently discard them.
//
// Replay finds that exact line again in the retained files and continues
// after it. When it cannot be found, everything between the cursor and
// the oldest retained file is gone, which the forwarder reports as a gap.
type replayCursor struct {
	path         string
	saveInterval time.Duration
	logf         func(format string, args ...any)
	onError      func(error)

	mu       sync.Mutex
	loaded   bool // a cursor file existed at startup, whether or not it parsed
	valid    bool // lastHash names a line that was processed
	lastTime time.Time
	lastHash string
	dirty    bool
	lastSave time.Time
}

type cursorState struct {
	LastTime string `json:"last_time"`
	LastHash string `json:"last_hash"`
}

func newReplayCursor(path string, logf func(format string, args ...any)) *replayCursor {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &replayCursor{
		path:         path,
		saveInterval: time.Second,
		logf:         logf,
		lastSave:     time.Now(),
	}
}

// load restores the cursor and reports whether it names a usable
// boundary. A missing file is a first start; a corrupt file leaves the
// cursor loaded but without a boundary, which forces a replay and a gap
// alert instead of silently starting at EOF.
func (c *replayCursor) load() bool {
	if c == nil || c.path == "" {
		return false
	}
	data, err := os.ReadFile(c.path)
	if errors.Is(err, os.ErrNotExist) {
		return false
	}
	if err != nil {
		c.logf("read cursor: %v; replaying retained logs", err)
		c.markUnknown()
		return false
	}
	var state cursorState
	if err := json.Unmarshal(data, &state); err != nil {
		c.logf("parse cursor: %v; replaying retained logs", err)
		c.markUnknown()
		return false
	}
	t, err := time.Parse(time.RFC3339Nano, state.LastTime)
	if err != nil || state.LastHash == "" {
		c.logf("invalid cursor state; replaying retained logs")
		c.markUnknown()
		return false
	}
	c.mu.Lock()
	c.loaded = true
	c.valid = true
	c.lastTime = t.UTC()
	c.lastHash = state.LastHash
	c.mu.Unlock()
	return true
}

// markUnknown records that a cursor existed but its position is lost.
func (c *replayCursor) markUnknown() {
	c.mu.Lock()
	c.loaded = true
	c.valid = false
	c.mu.Unlock()
}

// hasState reports whether a cursor file existed at startup. It is used
// to decide whether the first-run startup grace applies.
func (c *replayCursor) hasState() bool {
	if c == nil || c.path == "" {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.loaded
}

// boundary returns the last processed line and its event time.
func (c *replayCursor) boundary() (time.Time, string, bool) {
	if c == nil || c.path == "" {
		return time.Time{}, "", false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.valid {
		return time.Time{}, "", false
	}
	return c.lastTime, c.lastHash, true
}

// advance records the line just processed. An empty hash is ignored:
// only a real line may move the boundary.
func (c *replayCursor) advance(t time.Time, hash string) {
	if c == nil || c.path == "" || hash == "" {
		return
	}
	c.mu.Lock()
	c.loaded = true
	c.valid = true
	c.lastTime = t
	c.lastHash = hash
	c.dirty = true
	if time.Since(c.lastSave) >= c.saveInterval {
		c.reportSaveError(c.saveLocked())
	}
	c.mu.Unlock()
}

func (c *replayCursor) flush() {
	if c == nil || c.path == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.dirty {
		return
	}
	c.reportSaveError(c.saveLocked())
}

// reportSaveError surfaces a cursor that cannot be persisted. It is not
// fatal (the spool is still durable), but a restart will replay from an
// older boundary, so the failure is exported as a metric.
func (c *replayCursor) reportSaveError(err error) {
	if err == nil {
		return
	}
	c.logf("save cursor: %v", err)
	if c.onError != nil {
		c.onError(err)
	}
}

func (c *replayCursor) saveLocked() error {
	if !c.valid {
		return nil
	}
	state := cursorState{
		LastTime: c.lastTime.Format(time.RFC3339Nano),
		LastHash: c.lastHash,
	}
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(c.path), 0o750); err != nil {
		return err
	}
	if err := writeFileAtomic(c.path, data, 0o600); err != nil {
		return fmt.Errorf("replace cursor: %w", err)
	}
	c.dirty = false
	c.lastSave = time.Now()
	return nil
}
