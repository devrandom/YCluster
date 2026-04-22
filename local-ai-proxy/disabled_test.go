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

// fakeDisabled is a test double for DisabledLookup.
type fakeDisabled struct {
	disabled map[string]bool
	refreshes atomic.Int32
}

func (f *fakeDisabled) IsDisabled(u string) bool { return f.disabled[u] }
func (f *fakeDisabled) Refresh(ctx context.Context) error {
	f.refreshes.Add(1)
	return nil
}

func TestHealthCheckerSkipsDisabledBackend(t *testing.T) {
	var hits atomic.Int32
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()
	disabledURL := mustURL(t, up.URL)

	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {disabledURL},
	}}
	hc := NewHealthChecker(src, 10*time.Millisecond, logger)
	hc.Disabled = &fakeDisabled{disabled: map[string]bool{disabledURL.String(): true}}

	hc.checkAll(context.Background())

	if hits.Load() != 0 {
		t.Errorf("upstream was polled %d times; want 0 (disabled)", hits.Load())
	}
	bh := hc.Snapshot()[disabledURL.String()]
	if bh.State != StateDisabled {
		t.Errorf("state = %v; want disabled", bh.State)
	}
}

func TestHealthCheckerRefreshesDisabledEachCycle(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	src := &fakeSource{m: map[string][]*url.URL{}}
	fd := &fakeDisabled{disabled: map[string]bool{}}
	hc := NewHealthChecker(src, 10*time.Millisecond, logger)
	hc.Disabled = fd

	hc.checkAll(context.Background())
	hc.checkAll(context.Background())
	hc.checkAll(context.Background())

	if got := fd.refreshes.Load(); got != 3 {
		t.Errorf("refresh called %d times; want 3", got)
	}
}

func TestHealthCheckerUnDisableTransitionsBackToHealthy(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()
	u := mustURL(t, up.URL)

	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	src := &fakeSource{m: map[string][]*url.URL{"alpha": {u}}}
	fd := &fakeDisabled{disabled: map[string]bool{u.String(): true}}
	hc := NewHealthChecker(src, 10*time.Millisecond, logger)
	hc.Disabled = fd

	hc.checkAll(context.Background())
	if s := hc.Snapshot()[u.String()].State; s != StateDisabled {
		t.Fatalf("state after disable = %v; want disabled", s)
	}

	// Un-disable
	fd.disabled = map[string]bool{}
	hc.checkAll(context.Background())
	if s := hc.Snapshot()[u.String()].State; s != StateHealthy {
		t.Errorf("state after enable = %v; want healthy", s)
	}
}
