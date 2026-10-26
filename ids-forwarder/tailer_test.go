package main

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func waitFor(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}

func TestTailerAppendsAndPartialLines(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.json")
	if err := os.WriteFile(path, []byte(`{"a":1}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var got [][]byte
	tl, err := newTailer(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, append([]byte(nil), line...))
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()

	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	// Partial write first, then the rest — must arrive as one line.
	f.WriteString(`{"b":`)
	f.Sync()
	time.Sleep(20 * time.Millisecond)
	f.WriteString(`2}` + "\n")
	f.Close()

	// Several appends between polls: none may be skipped.
	for i := 0; i < 5; i++ {
		f2, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
		f2.WriteString(`{"n":` + string(rune('0'+i)) + "}\n")
		f2.Close()
		time.Sleep(2 * time.Millisecond)
	}

	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) >= 7
	})
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 7 {
		t.Fatalf("expected 7 lines, got %d: %v", len(got), got)
	}
	if string(got[0]) != `{"a":1}` {
		t.Fatalf("first line: %q", got[0])
	}
	if string(got[1]) != `{"b":2}` {
		t.Fatalf("partial line not reassembled: %q", got[1])
	}
	for i := 0; i < 5; i++ {
		want := `{"n":` + string(rune('0'+i)) + `}`
		if string(got[2+i]) != want {
			t.Fatalf("line %d: got %q want %q", i, got[2+i], want)
		}
	}
}

func TestTailerRotation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.json")
	if err := os.WriteFile(path, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var got []string
	tl, err := newTailer(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, string(line))
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()

	// Wait until "old" was consumed, then rotate: rename + new file.
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new1\nnew2\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 3
	})
	mu.Lock()
	defer mu.Unlock()
	if got[1] != "new1" || got[2] != "new2" {
		t.Fatalf("rotation lines: %v", got[1:])
	}
}

// TestTailerRotationWithUnreadTail is the adversarial case the review
// flagged: the file is renamed and replaced between polls while old-file
// lines are still unread. Every old line must be delivered before any
// new-file line.
func TestTailerRotationWithUnreadTail(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.json")
	if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var got []string
	tl, err := newTailer(path, 50*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, string(line))
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()

	// Append several lines quickly and rotate before the tailer's next
	// poll (50ms) can drain them.
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	for i := 0; i < 5; i++ {
		f.WriteString("old" + string(rune('0'+i)) + "\n")
	}
	f.Close()
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	waitFor(t, 3*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) >= 6
	})
	mu.Lock()
	defer mu.Unlock()
	for i := 0; i < 5; i++ {
		if got[i] != "old"+string(rune('0'+i)) {
			t.Fatalf("old line %d lost or reordered: %v", i, got)
		}
	}
	if got[5] != "new" {
		t.Fatalf("new-file line wrong: %v", got)
	}
}

func TestTailerTruncation(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.json")
	if err := os.WriteFile(path, []byte("first\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var got []string
	tl, err := newTailer(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, string(line))
	})
	if err != nil {
		t.Fatal(err)
	}
	go tl.run()
	defer tl.close()

	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	// Truncate back to empty and append fresh content. Sleep between so a
	// poll can observe the truncated size (otherwise a same-size append
	// masks the truncation — a known limitation shared by tail(1)).
	if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond)
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	f.WriteString("after\n")
	f.Close()

	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 2
	})
	mu.Lock()
	defer mu.Unlock()
	if got[1] != "after" {
		t.Fatalf("post-truncate line: %q", got[1])
	}
}

func TestTailerMissingFileThenCreated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "events.json")
	// Start with no file: newTailer should fail cleanly.
	if _, err := newTailer(path, 5*time.Millisecond, func([]byte) {}); err == nil {
		t.Fatal("expected error for missing file")
	}
	// startTailerWithRetry allows a missing file and picks it up later.
	var mu sync.Mutex
	var got []string
	tl := startTailerWithRetry(path, 5*time.Millisecond, func(line []byte) {
		mu.Lock()
		defer mu.Unlock()
		got = append(got, string(line))
	})
	defer tl.close()
	time.Sleep(20 * time.Millisecond)
	if err := os.WriteFile(path, []byte("late\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 2*time.Second, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(got) == 1
	})
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || got[0] != "late" {
		t.Fatalf("retry tailer missed the late file: %v", got)
	}
}
