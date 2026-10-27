package main

import (
	"os"
	"sync"
	"time"
)

// tailer tails a JSON-lines file, tolerating appends, truncation and
// rotation. It keeps the read offset across EOF so no appended line is
// skipped, buffers partial lines until a newline arrives, and only reopens
// when the inode changes (rotation) or the file shrinks (truncation).
const tailChunkBytes = 64 * 1024

type tailer struct {
	path       string
	poll       time.Duration
	onLine     func([]byte)
	onRotate   func()
	startAtEnd bool

	mu         sync.Mutex
	f          *os.File
	pathDev    uint64 // inode identity across rename
	pathIno    uint64
	offset     int64
	partial    []byte
	closed     bool
	openedOnce bool
}

func newTailer(path string, poll time.Duration, onLine func([]byte)) (*tailer, error) {
	return newTailerWithOptions(path, poll, false, onLine)
}

func newTailerAtEnd(path string, poll time.Duration, onLine func([]byte)) (*tailer, error) {
	return newTailerWithOptions(path, poll, true, onLine)
}

func newTailerWithOptions(path string, poll time.Duration, startAtEnd bool, onLine func([]byte)) (*tailer, error) {
	t := &tailer{path: path, poll: poll, onLine: onLine, startAtEnd: startAtEnd}
	if err := t.open(); err != nil {
		return nil, err
	}
	return t, nil
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
		rotated := t.readOnce()
		if rotated {
			// Reopen first, then let the replay hook scan every file that
			// exists now. If the hook blocks on backpressure, the next
			// rotation pass catches any files created meanwhile.
			_ = t.reopen()
			if t.onRotate != nil {
				t.onRotate()
			}
		}
		time.Sleep(t.poll)
	}
}

// readOnce returns true when the open handle must be replaced: rotation,
// truncation, or a read/stat error. The caller invokes onRotate outside
// the tailer lock, then reopens the current path.
func (t *tailer) readOnce() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.f == nil {
		// The previous open failed (file briefly missing after rotation);
		// keep retrying so the tailer recovers when the file reappears.
		if err := t.open(); err != nil {
			return false
		}
	}
	// Drain everything currently available from the open handle BEFORE
	// checking for rotation, so a rename+replace between polls never
	// discards unread tail bytes of the old file. Draining to EOF also
	// lets the startup replay gate skip a large already-processed prefix
	// in one pass instead of waiting a poll interval per 64 KiB.
	for {
		data, err := readAvailable(t.f, tailChunkBytes)
		if err != nil {
			t.closeFileLocked()
			return true
		}
		if len(data) == 0 {
			break
		}
		t.consume(data)
		if len(data) < tailChunkBytes {
			break
		}
	}
	info, err := t.f.Stat()
	if err != nil {
		t.closeFileLocked()
		return true
	}
	if t.rotated(info) {
		t.closeFileLocked()
		return true
	}
	if t.truncated(info) {
		if err := t.reset(); err != nil {
			t.closeFileLocked()
			return true
		}
	}
	return false
}

func (t *tailer) closeFileLocked() {
	if t.f != nil {
		t.f.Close()
		t.f = nil
	}
}
