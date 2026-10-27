package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

type spoolItem struct {
	alert alert
	token int64
}

// next returns the oldest unacked alert. A corrupt or oversized record is
// consumed and reported so one bad line cannot wedge the queue.
func (s *alertSpool) next() (spoolItem, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ack >= s.size {
		return spoolItem{}, false, nil
	}
	line, end, err := s.readLineLocked(s.ack)
	if errors.Is(err, errSpoolEntryTooLarge) {
		end, skipErr := s.skipLineLocked(s.ack)
		if skipErr != nil {
			return spoolItem{}, false, skipErr
		}
		if ackErr := s.ackNowLocked(end); ackErr != nil {
			return spoolItem{}, false, ackErr
		}
		return spoolItem{}, false, err
	}
	if err != nil {
		return spoolItem{}, false, err
	}
	var a alert
	if err := json.Unmarshal(line, &a); err != nil {
		offset := s.ack
		if ackErr := s.ackNowLocked(end); ackErr != nil {
			return spoolItem{}, false, ackErr
		}
		return spoolItem{}, false, fmt.Errorf("%w at offset %d: %v", errSpoolCorrupt, offset, err)
	}
	return spoolItem{alert: a, token: end}, true, nil
}

func (s *alertSpool) readLineLocked(pos int64) ([]byte, int64, error) {
	buf := make([]byte, 0, 4096)
	chunk := make([]byte, 64*1024)
	for {
		n, err := s.f.ReadAt(chunk, pos)
		if n > 0 {
			if idx := bytes.IndexByte(chunk[:n], '\n'); idx >= 0 {
				if len(buf)+idx > spoolMaxEntryBytes {
					return nil, 0, errSpoolEntryTooLarge
				}
				buf = append(buf, chunk[:idx]...)
				return buf, pos + int64(idx) + 1, nil
			}
			buf = append(buf, chunk[:n]...)
			pos += int64(n)
			if len(buf) > spoolMaxEntryBytes {
				return nil, 0, errSpoolEntryTooLarge
			}
		}
		if err == io.EOF {
			return nil, 0, io.ErrUnexpectedEOF
		}
		if err != nil {
			return nil, 0, err
		}
	}
}

// skipLineLocked finds the end of an oversized record without holding it
// in memory.
func (s *alertSpool) skipLineLocked(pos int64) (int64, error) {
	chunk := make([]byte, 64*1024)
	for pos < s.size {
		n, err := s.f.ReadAt(chunk, pos)
		if n > 0 {
			if idx := bytes.IndexByte(chunk[:n], '\n'); idx >= 0 {
				return pos + int64(idx) + 1, nil
			}
			pos += int64(n)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return 0, err
		}
	}
	return 0, io.ErrUnexpectedEOF
}
