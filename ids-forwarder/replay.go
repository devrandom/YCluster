package main

import (
	"os"
	"time"
)

// replaySource is one retained Tetragon file. The live file is scanned
// through the descriptor the forwarder already holds, so a rename during
// replay cannot make the scan miss its tail or read a different inode.
type replaySource struct {
	path string
	dev  uint64
	ino  uint64
	held bool
}

// replayTetragonLogs replays retained Tetragon files after the durable
// cursor and returns how many bytes of the held file have been consumed,
// so the live tailer can resume at that exact offset.
func (f *forwarder) replayTetragonLogs(held *os.File) (int64, error) {
	if f.cursor == nil {
		return 0, nil
	}
	sources := f.replaySources(held)
	last, hash, ok := f.cursor.boundary()
	if ok {
		return f.replayFromBoundary(sources, held, last, hash)
	}
	return f.replayWithoutBoundary(sources, held, last)
}

// replayFromBoundary finds the last processed line and forwards what
// follows. A missing boundary means the restart window is exhausted;
// everything retained is replayed behind a gap alert.
func (f *forwarder) replayFromBoundary(sources []replaySource, held *os.File, last time.Time, hash string) (int64, error) {
	consumed, found, unreadable, err := f.replayAfterBoundary(sources, held, hash)
	if err != nil {
		return 0, err
	}
	if unreadable {
		if err := f.reportGap(last, sources, "a retained Tetragon file could not be read"); err != nil {
			return 0, err
		}
	}
	if !found {
		if err := f.reportGap(last, sources, "the retained Tetragon files no longer contain the last processed event"); err != nil {
			return 0, err
		}
		consumed, _, err = f.replayAll(sources, held, time.Time{})
		if err != nil {
			return 0, err
		}
	} else {
		f.resolveGap()
	}
	f.cursor.flush()
	return handoffOffset(consumed, held), nil
}

// replayWithoutBoundary covers a first start (bounded grace window) and a
// cursor whose position was lost (replay everything, report the gap).
func (f *forwarder) replayWithoutBoundary(sources []replaySource, held *os.File, last time.Time) (int64, error) {
	if !f.cursor.hasState() {
		floor := time.Time{}
		if f.cfg.startupGrace > 0 {
			floor = time.Now().UTC().Add(-f.cfg.startupGrace)
		}
		consumed, _, err := f.replayAll(sources, held, floor)
		if err != nil {
			return 0, err
		}
		f.cursor.flush()
		return handoffOffset(consumed, held), nil
	}
	if err := f.reportGap(last, sources, "the durable cursor could not be read"); err != nil {
		return 0, err
	}
	consumed, _, err := f.replayAll(sources, held, time.Time{})
	if err != nil {
		return 0, err
	}
	f.cursor.flush()
	return handoffOffset(consumed, held), nil
}
