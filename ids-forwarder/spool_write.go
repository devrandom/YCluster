package main

import (
	"encoding/json"
	"fmt"
	"hash/crc32"
	"io"
	"strconv"
)

// append stores one alert and returns only after the record is on disk.
func (s *alertSpool) append(a alert) error {
	data, err := json.Marshal(a)
	if err != nil {
		return err
	}
	if len(data) > spoolMaxEntryBytes {
		return fmt.Errorf("%w: %d bytes", errSpoolEntryTooLarge, len(data))
	}
	data = append(data, '\n')
	s.mu.Lock()
	defer s.mu.Unlock()
	for !s.closed && s.max > 0 && s.size-s.ack+int64(len(data)) > s.max {
		s.cond.Wait()
	}
	if s.closed || s.f == nil {
		return errSpoolClosed
	}
	n, err := s.f.WriteAt(data, s.size)
	if err != nil {
		return err
	}
	if n != len(data) {
		return io.ErrShortWrite
	}
	if err := s.f.Sync(); err != nil {
		return fmt.Errorf("sync spool: %w", err)
	}
	s.size += int64(n)
	s.entries++
	return nil
}

// ackTo records delivery of everything up to token.
func (s *alertSpool) ackTo(token int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if token <= s.ack {
		return nil
	}
	if token > s.size {
		token = s.size
	}
	s.ack = token
	if s.entries > 0 {
		s.entries--
	}
	if err := s.writeAckLocked(s.ack); err != nil {
		return err
	}
	s.cond.Broadcast()
	if s.ack >= s.size {
		return s.resetLocked()
	}
	if s.ack >= spoolCompactBytes && s.ack >= s.size/2 {
		return s.compactLocked()
	}
	return nil
}

// ackNowLocked moves the ack while mu is already held by the reader.
func (s *alertSpool) ackNowLocked(token int64) error {
	if token <= s.ack {
		return nil
	}
	if token > s.size {
		token = s.size
	}
	s.ack = token
	if s.entries > 0 {
		s.entries--
	}
	s.cond.Broadcast()
	if s.ack >= s.size {
		return s.resetLocked()
	}
	return s.writeAckLocked(s.ack)
}

// resetLocked empties a fully consumed spool.
func (s *alertSpool) resetLocked() error {
	if err := s.f.Truncate(0); err != nil {
		return err
	}
	if err := s.f.Sync(); err != nil {
		return err
	}
	s.ack = 0
	s.size = 0
	s.entries = 0
	if err := s.writeAckLocked(0); err != nil {
		return err
	}
	s.cond.Broadcast()
	return nil
}

// acks are replaced atomically and carry a short anchor into the data
// file, so a stale or rewound ack cannot skip alerts after a truncation
// that kept the file size similar.
type spoolAck struct {
	Offset int64  `json:"offset"`
	Anchor string `json:"anchor"`
}

func (s *alertSpool) writeAckLocked(offset int64) error {
	ack := spoolAck{Offset: offset}
	if offset > 0 {
		ack.Anchor = s.anchorLocked(offset)
	}
	data, err := json.Marshal(ack)
	if err != nil {
		return err
	}
	return writeFileAtomic(s.ackPath, data, 0o600)
}

// anchorLocked fingerprints the bytes immediately before offset.
func (s *alertSpool) anchorLocked(offset int64) string {
	start := offset - spoolAnchorBytes
	if start < 0 {
		start = 0
	}
	buf := make([]byte, offset-start)
	if _, err := s.f.ReadAt(buf, start); err != nil && err != io.EOF {
		return ""
	}
	return strconv.FormatUint(uint64(crc32.ChecksumIEEE(buf)), 16)
}
