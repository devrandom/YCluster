package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestLoggingMiddlewareEmitsStructuredLine(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo}))

	handler := LoggingMiddleware(logger, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTeapot)
		_, _ = io.WriteString(w, "hello")
	}))

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	req.Header.Set("X-User-Id", "alice")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	var entry map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(buf.Bytes()), &entry); err != nil {
		t.Fatalf("log output is not JSON: %v\n%s", err, buf.String())
	}

	cases := []struct {
		key  string
		want any
	}{
		{"method", "POST"},
		{"path", "/v1/chat/completions"},
		{"status", float64(http.StatusTeapot)},
		{"user", "alice"},
		{"bytes_out", float64(5)},
	}
	for _, c := range cases {
		if got := entry[c.key]; got != c.want {
			t.Errorf("log[%q] = %v (%T); want %v", c.key, got, got, c.want)
		}
	}
	if _, ok := entry["duration_ms"]; !ok {
		t.Error("log missing duration_ms")
	}
}

func TestLoggingMiddlewareDefaultStatusIsOK(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&buf, nil))
	handler := LoggingMiddleware(logger, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	handler.ServeHTTP(httptest.NewRecorder(), req)

	var entry map[string]any
	_ = json.Unmarshal(bytes.TrimSpace(buf.Bytes()), &entry)
	if entry["status"] != float64(http.StatusOK) {
		t.Errorf("status when handler didn't call WriteHeader: %v; want 200", entry["status"])
	}
}

// TestLoggingMiddlewarePreservesFlush verifies that wrapping the handler
// in LoggingMiddleware doesn't break the streaming path. If statusWriter
// didn't implement Unwrap, http.NewResponseController would fail to find
// the underlying Flusher and streaming would buffer.
func TestLoggingMiddlewarePreservesFlush(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))

	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rc := http.NewResponseController(w)
		_, _ = io.WriteString(w, "chunk-1")
		if err := rc.Flush(); err != nil {
			t.Errorf("flush: %v", err)
		}
		<-r.Context().Done()
	})

	srv := httptest.NewServer(LoggingMiddleware(logger, inner))
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	got, err := readChunkWithTimeout(resp.Body, 2*time.Second)
	if err != nil {
		t.Fatalf("did not receive flushed chunk: %v", err)
	}
	if !strings.Contains(string(got), "chunk-1") {
		t.Errorf("got %q; want it to contain %q", got, "chunk-1")
	}
}
