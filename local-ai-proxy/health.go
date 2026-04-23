package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// BackendState is the health state of a single backend URL.
type BackendState int

const (
	StateUnknown BackendState = iota
	StateHealthy
	StateDown
	StateDisabled // operator-marked as known-down; not polled, not alerted
)

func (s BackendState) String() string {
	switch s {
	case StateHealthy:
		return "healthy"
	case StateDown:
		return "down"
	case StateDisabled:
		return "disabled"
	default:
		return "unknown"
	}
}

// BackendHealth snapshots a backend's last-known check result.
type BackendHealth struct {
	URL       *url.URL
	State     BackendState
	LastCheck time.Time
	Err       string // set when state is Down
}

// DisabledLookup returns true when the given backend URL is operator-
// marked as known-down and should be skipped by health checks.
type DisabledLookup interface {
	IsDisabled(url string) bool
	Refresh(ctx context.Context) error
}

// HealthChecker polls each unique backend URL from a Source on a fixed
// interval. Results drive observability today; a future router will
// consult them when fan-out arrives.
type HealthChecker struct {
	source    Source
	interval  time.Duration
	client    *http.Client
	logger    *slog.Logger

	// Disabled, if set, is consulted before each poll. Disabled URLs
	// are not contacted and are reported with StateDisabled.
	Disabled DisabledLookup

	mu     sync.RWMutex
	states map[string]BackendHealth // keyed by url.String()

	cancel context.CancelFunc
	done   chan struct{}
}

func NewHealthChecker(source Source, interval time.Duration, logger *slog.Logger) *HealthChecker {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DisableKeepAlives = true
	return &HealthChecker{
		source:   source,
		interval: interval,
		client: &http.Client{
			Transport: transport,
			Timeout:   5 * time.Second,
		},
		logger: logger,
		states: make(map[string]BackendHealth),
	}
}

// Start begins the periodic check loop in a goroutine. The first check
// runs immediately so Snapshot() is populated by the time Start returns.
func (hc *HealthChecker) Start(parent context.Context) {
	ctx, cancel := context.WithCancel(parent)
	hc.cancel = cancel
	hc.done = make(chan struct{})

	hc.checkAll(ctx)

	go func() {
		defer close(hc.done)
		ticker := time.NewTicker(hc.interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				hc.checkAll(ctx)
			}
		}
	}()
}

func (hc *HealthChecker) Close() error {
	if hc.cancel != nil {
		hc.cancel()
	}
	if hc.done != nil {
		<-hc.done
	}
	return nil
}

// Probe runs an out-of-band health check on u in a goroutine and
// returns immediately. Used by the request handler to nudge the
// checker after a retry: the checker is the single writer to the
// state map, and an immediate re-probe means a transient backend
// glitch doesn't strand traffic until the next tick.
//
// Disabled backends are skipped. If the checker is shutting down
// the probe is a no-op.
func (hc *HealthChecker) Probe(u *url.URL) {
	if hc == nil || u == nil {
		return
	}
	if hc.Disabled != nil && hc.Disabled.IsDisabled(u.String()) {
		return
	}
	ctx := context.Background()
	if hc.done != nil {
		select {
		case <-hc.done:
			return
		default:
		}
	}
	go hc.checkOne(ctx, u)
}

// IsHealthy reports whether the given backend URL was observed as
// StateHealthy on the most recent check. Backends that are Unknown
// (e.g. added between ticks), Down, or Disabled return false — the
// router should exclude them.
func (hc *HealthChecker) IsHealthy(urlStr string) bool {
	hc.mu.RLock()
	defer hc.mu.RUnlock()
	bh, ok := hc.states[urlStr]
	return ok && bh.State == StateHealthy
}

// Snapshot returns a copy of the current per-backend states.
func (hc *HealthChecker) Snapshot() map[string]BackendHealth {
	hc.mu.RLock()
	defer hc.mu.RUnlock()
	out := make(map[string]BackendHealth, len(hc.states))
	for k, v := range hc.states {
		out[k] = v
	}
	return out
}

// checkAll runs one round of checks against every unique backend URL
// currently in source.Snapshot(). Exposed for tests.
func (hc *HealthChecker) checkAll(ctx context.Context) {
	if hc.Disabled != nil {
		if err := hc.Disabled.Refresh(ctx); err != nil {
			hc.logger.Warn("disabled-set refresh failed", "err", err.Error())
		}
	}

	urls := hc.uniqueBackendURLs()
	hc.mu.Lock()
	// Drop state entries for URLs that have disappeared from the source.
	for k := range hc.states {
		if _, still := urls[k]; !still {
			delete(hc.states, k)
		}
	}
	hc.mu.Unlock()

	for k, u := range urls {
		if hc.Disabled != nil && hc.Disabled.IsDisabled(k) {
			hc.record(u, StateDisabled, "")
			continue
		}
		hc.checkOne(ctx, u)
	}
}

// uniqueBackendURLs returns the set of distinct backend URLs across all
// models, keyed by url.String() for stable comparison.
func (hc *HealthChecker) uniqueBackendURLs() map[string]*url.URL {
	snap := hc.source.Snapshot()
	out := make(map[string]*url.URL)
	for _, urls := range snap {
		for _, u := range urls {
			out[u.String()] = u
		}
	}
	return out
}

func (hc *HealthChecker) checkOne(ctx context.Context, u *url.URL) {
	checkURL := *u
	checkURL.Path = joinPath(checkURL.Path, "/v1/models")

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, checkURL.String(), nil)
	if err != nil {
		hc.record(u, StateDown, fmt.Sprintf("build request: %v", err))
		return
	}
	resp, err := hc.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return // shutting down
		}
		hc.record(u, StateDown, err.Error())
		return
	}
	resp.Body.Close()

	// Treat any HTTP response as "backend is reachable" — a 4xx from
	// auth or a missing endpoint still means the server is up. 5xx is
	// counted as down.
	if resp.StatusCode >= 500 {
		hc.record(u, StateDown, fmt.Sprintf("HTTP %d", resp.StatusCode))
		return
	}
	hc.record(u, StateHealthy, "")
}

func (hc *HealthChecker) record(u *url.URL, state BackendState, errMsg string) {
	hc.mu.Lock()
	prev, had := hc.states[u.String()]
	next := BackendHealth{
		URL:       u,
		State:     state,
		LastCheck: time.Now(),
		Err:       errMsg,
	}
	hc.states[u.String()] = next
	hc.mu.Unlock()

	if !had || prev.State != state {
		msg := "backend healthy"
		lvl := slog.LevelInfo
		switch state {
		case StateDown:
			msg = "backend down"
			lvl = slog.LevelWarn
		case StateDisabled:
			msg = "backend disabled"
			lvl = slog.LevelInfo
		}
		attrs := []any{"backend", u.String()}
		if errMsg != "" {
			attrs = append(attrs, "err", errMsg)
		}
		hc.logger.Log(context.Background(), lvl, msg, attrs...)
	}
}
