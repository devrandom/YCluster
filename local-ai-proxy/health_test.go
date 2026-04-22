package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"
)

func newTestHealthChecker(t *testing.T, m map[string][]*url.URL) *HealthChecker {
	t.Helper()
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	return NewHealthChecker(&fakeSource{m: m}, 10*time.Millisecond, logger)
}

func TestHealthCheckerMarksUpstreamsHealthy(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("upstream got path %q; want /v1/models", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	snap := hc.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("got %d states; want 1", len(snap))
	}
	bh, ok := snap[up.URL]
	if !ok {
		t.Fatalf("no state for %s", up.URL)
	}
	if bh.State != StateHealthy {
		t.Errorf("state = %v; want healthy", bh.State)
	}
	if bh.Err != "" {
		t.Errorf("err = %q; want empty", bh.Err)
	}
	if bh.LastCheck.IsZero() {
		t.Error("LastCheck not set")
	}
}

func TestHealthCheckerMarksUnreachableDown(t *testing.T) {
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close() // guaranteed refuse

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, dead.URL)},
	})
	hc.checkAll(context.Background())

	bh := hc.Snapshot()[dead.URL]
	if bh.State != StateDown {
		t.Errorf("state = %v; want down", bh.State)
	}
	if bh.Err == "" {
		t.Error("Err should describe the failure")
	}
}

func TestHealthChecker4xxCountsAsHealthy(t *testing.T) {
	// Some backends require auth and return 401 to unauthenticated
	// /v1/models. That still means the backend is alive.
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	if s := hc.Snapshot()[up.URL].State; s != StateHealthy {
		t.Errorf("state for 401 = %v; want healthy", s)
	}
}

func TestHealthChecker5xxCountsAsDown(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	if s := hc.Snapshot()[up.URL].State; s != StateDown {
		t.Errorf("state for 503 = %v; want down", s)
	}
}

func TestHealthCheckerTracksStateChanges(t *testing.T) {
	var alive atomic.Bool
	alive.Store(true)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if alive.Load() {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})

	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateHealthy {
		t.Fatal("initial state should be healthy")
	}

	alive.Store(false)
	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateDown {
		t.Fatal("state should have flipped to down")
	}

	alive.Store(true)
	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateHealthy {
		t.Fatal("state should have flipped back to healthy")
	}
}

func TestHealthCheckerDropsStaleEntries(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	source := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	}}
	hc := NewHealthChecker(source, time.Millisecond, slog.New(slog.NewJSONHandler(io.Discard, nil)))

	hc.checkAll(context.Background())
	if _, ok := hc.Snapshot()[up.URL]; !ok {
		t.Fatal("state should exist after first check")
	}

	// Remove the model from the source.
	source.m = map[string][]*url.URL{}
	hc.checkAll(context.Background())
	if _, ok := hc.Snapshot()[up.URL]; ok {
		t.Fatal("stale entry should have been dropped")
	}
}

func TestHealthCheckerDeduplicatesURLsAcrossModels(t *testing.T) {
	var hits atomic.Int32
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {u},
		"beta":  {u}, // same URL, different model
		"gamma": {u},
	})
	hc.checkAll(context.Background())

	if hits.Load() != 1 {
		t.Errorf("upstream hit %d times; want 1 (one check per unique URL)", hits.Load())
	}
	if len(hc.Snapshot()) != 1 {
		t.Errorf("got %d state entries; want 1", len(hc.Snapshot()))
	}
}
