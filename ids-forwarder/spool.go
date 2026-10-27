package main

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
)

// spoolError marks a failure of the durable queue, as opposed to a bad
// or unreadable input file during replay.
type spoolError struct{ err error }

func (e *spoolError) Error() string { return e.err.Error() }
func (e *spoolError) Unwrap() error { return e.err }

var (
	errSpoolClosed        = errors.New("alert spool closed")
	errSpoolEntryTooLarge = errors.New("spool entry exceeds the size cap")
	errSpoolCorrupt       = errors.New("corrupt spool entry")
)

const (
	// spoolMaxEntryBytes bounds one JSONL record. It is well below the
	// line scanner cap so a pathological event is dropped explicitly
	// instead of wedging the queue.
	spoolMaxEntryBytes = 4 << 20
	// spoolCompactBytes is the acked prefix that triggers compaction.
	spoolCompactBytes = 1 << 20
	// spoolAnchorBytes is the window used to detect a truncated or
	// rewound data file under a stale ack offset.
	spoolAnchorBytes = 64
)

// alertSpool is a bounded, append-only JSONL queue. The data file is
// fsynced before append returns, so the caller may advance its replay
// cursor: a crash after that point leaves the alert on disk. The ack file
// records how far the post loop has consumed. Losing an ack only replays
// an alert (at-least-once); it never drops one.
type alertSpool struct {
	path    string
	ackPath string
	max     int64
	logf    func(format string, args ...any)

	mu      sync.Mutex
	cond    *sync.Cond
	f       *os.File
	size    int64
	ack     int64
	entries int
	closed  bool
}

func openAlertSpool(path string, max int64, logf func(format string, args ...any)) (*alertSpool, error) {
	if path == "" {
		return nil, nil
	}
	if logf == nil {
		logf = func(string, ...any) {}
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, err
	}
	_, statErr := os.Stat(path)
	created := errors.Is(statErr, os.ErrNotExist)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if created {
		// Make the new file's directory entry durable before a cursor
		// can point at alerts stored in it.
		if err := f.Sync(); err != nil {
			f.Close()
			return nil, err
		}
		if err := syncDir(dir); err != nil {
			f.Close()
			return nil, err
		}
	}
	s := &alertSpool{
		path:    path,
		ackPath: path + ".ack",
		max:     max,
		logf:    logf,
		f:       f,
	}
	s.cond = sync.NewCond(&s.mu)
	if err := s.recover(); err != nil {
		f.Close()
		return nil, err
	}
	return s, nil
}

func (s *alertSpool) depth() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.entries
}

func (s *alertSpool) pendingBytes() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.size - s.ack
}

func (s *alertSpool) close() {
	if s == nil {
		return
	}
	s.stop()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.f != nil {
		s.f.Close()
		s.f = nil
	}
}

// stop unblocks a writer waiting for spool capacity. Closing the file is
// deferred until the post loop has exited.
func (s *alertSpool) stop() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.closed = true
	s.cond.Broadcast()
	s.mu.Unlock()
}
