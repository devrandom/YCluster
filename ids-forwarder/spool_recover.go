package main

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
)

func (s *alertSpool) recover() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	info, err := s.f.Stat()
	if err != nil {
		return err
	}
	s.size = info.Size()
	s.ack = s.readAckLocked()
	if err := s.trimPartialLocked(); err != nil {
		return err
	}
	if s.ack >= s.size {
		return s.resetLocked()
	}
	if s.ack > 0 && (s.ack >= spoolCompactBytes || s.ack >= s.size/2) {
		return s.compactLocked()
	}
	s.entries = s.countEntriesLocked()
	return nil
}

// readAckLocked loads the ack and rejects it when it does not match the
// data file. Falling back to zero replays alerts but never skips them.
func (s *alertSpool) readAckLocked() int64 {
	data, err := os.ReadFile(s.ackPath)
	if err != nil {
		return 0
	}
	var ack spoolAck
	if err := json.Unmarshal(data, &ack); err != nil {
		return 0
	}
	if ack.Offset <= 0 || ack.Offset > s.size {
		return 0
	}
	if ack.Anchor != s.anchorLocked(ack.Offset) {
		s.logf("spool ack does not match the data file; replaying from the start")
		return 0
	}
	return ack.Offset
}

// trimPartialLocked discards a torn final line from a crash during append.
// Complete lines before it remain valid. The scan walks backwards in
// chunks so a full 100 MB spool does not need a matching allocation.
func (s *alertSpool) trimPartialLocked() error {
	if s.size <= s.ack {
		return nil
	}
	const chunk = 1 << 20
	buf := make([]byte, chunk)
	pos := s.size
	for pos > s.ack {
		n := int64(chunk)
		if pos-n < s.ack {
			n = pos - s.ack
		}
		if _, err := s.f.ReadAt(buf[:n], pos-n); err != nil && err != io.EOF {
			return err
		}
		if idx := bytes.LastIndexByte(buf[:n], '\n'); idx >= 0 {
			end := pos - n + int64(idx) + 1
			if end == s.size {
				return nil
			}
			return s.truncateLocked(end)
		}
		pos -= n
	}
	return s.truncateLocked(s.ack)
}

func (s *alertSpool) truncateLocked(end int64) error {
	if err := s.f.Truncate(end); err != nil {
		return err
	}
	if err := s.f.Sync(); err != nil {
		return err
	}
	s.size = end
	return nil
}

// newlineCounter counts JSONL records while they are copied.
type newlineCounter struct {
	w io.Writer
	n int
}

func (c *newlineCounter) Write(p []byte) (int, error) {
	c.n += bytes.Count(p, []byte{'\n'})
	return c.w.Write(p)
}

func (s *alertSpool) countEntriesLocked() int {
	n := 0
	buf := make([]byte, 1<<20)
	for pos := s.ack; pos < s.size; {
		want := int64(len(buf))
		if rem := s.size - pos; rem < want {
			want = rem
		}
		read, err := s.f.ReadAt(buf[:want], pos)
		n += bytes.Count(buf[:read], []byte{'\n'})
		pos += int64(read)
		if err != nil {
			break
		}
	}
	return n
}
