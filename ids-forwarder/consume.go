package main

import (
	"bytes"
	"io"
	"os"
	"time"
)

// consume splits newly read bytes into complete lines, keeping an
// incomplete trailing chunk buffered until the rest arrives.
func (t *tailer) consume(data []byte) {
	if len(data) == 0 {
		return
	}
	t.offset += int64(len(data))
	t.partial = append(t.partial, data...)
	for {
		idx := bytes.IndexByte(t.partial, '\n')
		if idx < 0 {
			break
		}
		line := bytes.TrimRight(t.partial[:idx], "\r")
		if len(line) > 0 {
			t.onLine(line)
		}
		t.partial = t.partial[idx+1:]
	}
}

// tailerForExisting tolerates a missing file: it retries opening until the
// file appears, then tails it.
func startTailerWithRetry(path string, poll time.Duration, onLine func([]byte)) *tailer {
	t := &tailer{path: path, poll: poll, onLine: onLine}
	go func() {
		for {
			t.mu.Lock()
			closed := t.closed
			t.mu.Unlock()
			if closed {
				return
			}
			t.mu.Lock()
			_ = t.open() // retry silently; readOnce handles nil f
			t.mu.Unlock()
			t.readOnce()
			time.Sleep(t.poll)
		}
	}()
	return t
}

func (t *tailer) reopen() {
	t.f.Close()
	t.f = nil
	if err := t.open(); err != nil {
		// Keep the offset and retry on the next poll.
		return
	}
	if t.onRotate != nil {
		t.onRotate()
	}
}

func readAvailable(f *os.File, max int) ([]byte, error) {
	buf := make([]byte, max)
	n, err := f.Read(buf)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:n], nil
}
