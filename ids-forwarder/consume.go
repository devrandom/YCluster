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
// file appears, then tails it. The file is new, so start at its beginning
// rather than skipping events written while Tetragon was starting.
func startTailerWithRetry(path string, poll time.Duration, onLine func([]byte)) *tailer {
	t := newRetryTailer(path, poll, onLine)
	go t.run()
	return t
}

func newRetryTailer(path string, poll time.Duration, onLine func([]byte)) *tailer {
	return &tailer{path: path, poll: poll, onLine: onLine}
}

func (t *tailer) reopen() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.f != nil {
		t.f.Close()
		t.f = nil
	}
	return t.open()
}

func readAvailable(f *os.File, max int) ([]byte, error) {
	buf := make([]byte, max)
	n, err := f.Read(buf)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:n], nil
}
