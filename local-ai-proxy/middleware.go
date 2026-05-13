package main

import (
	"context"
	"log/slog"
	"net/http"
	"time"
)

// reqInfoKey carries per-request fields that downstream handlers
// populate for the logging middleware to read after ServeHTTP returns
// (e.g. the routed model name, which isn't known until Route() runs).
type reqInfoKey struct{}

type reqInfo struct {
	model string
}

func reqInfoFrom(ctx context.Context) *reqInfo {
	v, _ := ctx.Value(reqInfoKey{}).(*reqInfo)
	return v
}

// SetRequestModel records the routed model on the request context so
// LoggingMiddleware can include it in the access log. No-op if the
// context wasn't initialised by LoggingMiddleware.
func SetRequestModel(ctx context.Context, model string) {
	if info := reqInfoFrom(ctx); info != nil {
		info.model = model
	}
}

// statusWriter wraps http.ResponseWriter to capture status code and
// bytes written for logging. Implements Unwrap so that
// http.NewResponseController can reach the underlying Flusher (needed
// for streaming).
type statusWriter struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(p)
	w.bytes += int64(n)
	return n, err
}

func (w *statusWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

// LoggingMiddleware logs one structured record per request: method,
// path, status, duration, bytes out, X-User-Id and X-User-Groups (if
// present), and remote addr. Logger may be nil to use slog.Default().
func LoggingMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w}
		info := &reqInfo{}
		r = r.WithContext(context.WithValue(r.Context(), reqInfoKey{}, info))
		next.ServeHTTP(sw, r)
		logger.LogAttrs(r.Context(), slog.LevelInfo, "request",
			slog.String("method", r.Method),
			slog.String("path", r.URL.Path),
			slog.Int("status", sw.status),
			slog.Int64("duration_ms", time.Since(start).Milliseconds()),
			slog.Int64("bytes_out", sw.bytes),
			slog.String("user", r.Header.Get("X-User-Id")),
			slog.String("groups", r.Header.Get("X-User-Groups")),
			slog.String("remote", r.RemoteAddr),
			slog.String("model", info.model),
		)
	})
}
