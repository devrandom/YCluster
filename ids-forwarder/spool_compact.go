package main

import (
	"io"
	"os"
	"path/filepath"
)

// compactLocked rewrites the unacked tail so the file does not grow
// forever when the consumer keeps up slowly.
//
// Ordering matters. The replacement is written and fsynced first, then
// the ack is moved to zero while the old file is still in place, and only
// then is the name swapped. A crash before the rename leaves a complete
// old file whose ack replays from the start (duplicates, never loss). A
// crash after it leaves the replacement, whose ack is already correct.
func (s *alertSpool) compactLocked() error {
	remaining := s.size - s.ack
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".spool-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	tail := io.NewSectionReader(s.f, s.ack, remaining)
	counter := &newlineCounter{w: tmp}
	if _, err := io.Copy(counter, tail); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := s.writeAckLocked(0); err != nil {
		return err
	}
	if err := s.f.Close(); err != nil {
		return err
	}
	s.f = nil
	if err := os.Rename(tmpName, s.path); err != nil {
		// Leave the spool usable even if compaction failed.
		s.f, _ = os.OpenFile(s.path, os.O_RDWR, 0o600)
		return err
	}
	f, err := os.OpenFile(s.path, os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	s.f = f
	s.ack = 0
	s.size = remaining
	s.entries = counter.n
	if err := syncDir(filepath.Dir(s.path)); err != nil {
		return err
	}
	return nil
}
