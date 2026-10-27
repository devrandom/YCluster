package main

import (
	"io"
	"os"
	"time"
)

// newTailerFromFile adopts a file already opened by the caller. This
// closes the replay/tail handoff race: the descriptor is held before the
// replay scan, so a rotation during replay is still read from the old
// inode before the tailer notices the new path.
func newTailerFromFile(f *os.File, path string, poll time.Duration, offset int64, onLine func([]byte)) (*tailer, error) {
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	dev, ino := fileIdentity(info)
	if offset > info.Size() {
		offset = 0
	}
	t := &tailer{
		path:       path,
		poll:       poll,
		onLine:     onLine,
		f:          f,
		pathDev:    dev,
		pathIno:    ino,
		offset:     offset,
		openedOnce: true,
	}
	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		f.Close()
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
	if !t.openedOnce && t.startAtEnd {
		// First production start: do not replay a pre-existing Tetragon
		// file as if it were live. A missing file is handled by the retry
		// tailer, which starts at the beginning when Tetragon creates it.
		t.offset = info.Size()
	}
	t.openedOnce = true
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

func (t *tailer) rotated(info os.FileInfo) bool {
	st, err := os.Stat(t.path)
	if err != nil {
		return true
	}
	return !sameIdentity(t.pathDev, t.pathIno, st)
}

func (t *tailer) truncated(info os.FileInfo) bool {
	return info.Size() < t.offset
}

func (t *tailer) reset() error {
	t.offset = 0
	t.partial = t.partial[:0]
	_, err := t.f.Seek(0, io.SeekStart)
	return err
}
