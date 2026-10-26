package main

import (
	"io"
	"os"
	"sync"
	"time"
)

// tailer tails a JSON-lines file, tolerating appends, truncation and
// rotation. It keeps the read offset across EOF so no appended line is
// skipped, buffers partial lines until a newline arrives, and only reopens
// when the inode changes (rotation) or the file shrinks (truncation).
type tailer struct {
	path     string
	poll     time.Duration
	onLine   func([]byte)
	onRotate func()

	mu      sync.Mutex
	f       *os.File
	pathDev uint64 // inode identity across rename
	pathIno uint64
	offset  int64
	partial []byte
	closed  bool
}

func newTailer(path string, poll time.Duration, onLine func([]byte)) (*tailer, error) {
	t := &tailer{path: path, poll: poll, onLine: onLine}
	if err := t.open(); err != nil {
		return nil, err
	}
	return t, nil
}

func (t *tailer) close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.closed = true
	if t.f != nil {
		t.f.Close()
		t.f = nil
	}
}

func (t *tailer) open() error {
	f, err := os.Open(t.path)
	if err != nil {
		return err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return err
	}
	dev, ino := fileIdentity(info)
	rotated := t.f == nil && (t.pathDev != 0 || t.pathIno != 0) &&
		(dev != t.pathDev || ino != t.pathIno)
	t.f = f
	t.pathDev, t.pathIno = dev, ino
	if rotated {
		// New file after rotation: start from its beginning.
		t.offset = 0
		t.partial = t.partial[:0]
	}
	if t.offset > info.Size() {
		// Truncated or rotated: start over from the beginning.
		t.offset = 0
		t.partial = t.partial[:0]
	}
	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		f.Close()
		t.f = nil
		return err
	}
	return nil
}

// run blocks, polling the file until Close. Each newline-terminated chunk
// is passed to onLine; a partial trailing chunk stays buffered until the
// rest arrives.
func (t *tailer) run() {
	for {
		t.mu.Lock()
		closed := t.closed
		t.mu.Unlock()
		if closed {
			return
		}
		t.readOnce()
		time.Sleep(t.poll)
	}
}

func (t *tailer) readOnce() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.f == nil {
		// The previous open failed (file briefly missing after rotation);
		// keep retrying so the tailer recovers when the file reappears.
		if err := t.open(); err != nil {
			return
		}
	}
	// Drain everything currently available from the open handle BEFORE
	// checking for rotation, so a rename+replace between polls never
	// discards unread tail bytes of the old file.
	data, err := readAvailable(t.f, 64*1024)
	if err != nil {
		t.reopen()
		return
	}
	t.consume(data)
	info, err := t.f.Stat()
	if err != nil {
		t.reopen()
		return
	}
	if t.rotated(info) {
		t.reopen()
		return
	}
	if t.truncated(info) {
		if err := t.reset(); err != nil {
			t.reopen()
			return
		}
	}
}

// rotated reports whether the file at the configured path now has a
// different identity than the open handle (rename/replacement).
func (t *tailer) rotated(info os.FileInfo) bool {
	st, err := os.Stat(t.path)
	if err != nil {
		return true
	}
	return !sameIdentity(t.pathDev, t.pathIno, st)
}

// truncated reports whether the open file shrank below the read offset.
func (t *tailer) truncated(info os.FileInfo) bool {
	return info.Size() < t.offset
}

// reset seeks back to the start after a truncation and drops the
// buffered partial line.
func (t *tailer) reset() error {
	t.offset = 0
	t.partial = t.partial[:0]
	_, err := t.f.Seek(0, io.SeekStart)
	return err
}
