package main

import (
	"context"
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

	// Models is the list of model IDs the backend reported serving via
	// /v1/models. nil means the list couldn't be determined (e.g. 401,
	// non-JSON body) — ServesModel returns true in that case so the
	// router doesn't filter out a reachable backend. An empty (non-nil)
	// slice means the backend reported no models, so every model mapped
	// to it is stale and ServesModel returns false.
	Models []string
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
	source   Source
	interval time.Duration
	client   *http.Client
	logger   *slog.Logger

	// Disabled, if set, is consulted before each poll. Disabled URLs
	// are not contacted and are reported with StateDisabled.
	Disabled DisabledLookup

	// Metrics, if set, receives the backend-healthy gauge updates
	// whenever a check records a new state. Nil-safe.
	Metrics *Metrics

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
			// 15s tolerates backends (notably whisperx) that block on
			// /v1/models while a worker is busy transcribing — observed
			// ~3.7s under load with occasional spikes past 5s.
			Timeout: 15 * time.Second,
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

// ServesModel reports whether the given backend URL is known to serve
// the given model ID. Returns true when the model list couldn't be
// determined (Models is nil) so the router doesn't filter out a
// reachable backend we couldn't interrogate. Returns false when the
// backend reported a model list that doesn't contain the model — the
// stale-entry case this method exists to catch.
func (hc *HealthChecker) ServesModel(urlStr, model string) bool {
	hc.mu.RLock()
	defer hc.mu.RUnlock()
	bh, ok := hc.states[urlStr]
	if !ok || bh.Models == nil {
		return true
	}
	for _, m := range bh.Models {
		if m == model {
			return true
		}
	}
	return false
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

// updateModelMetrics recomputes per-model healthy/total backend counts
// from the current states and the source snapshot. Called after every
// state transition so the gauges also reflect model-set changes.
func (hc *HealthChecker) updateModelMetrics() {
	if hc.Metrics == nil {
		return
	}
	snap := hc.source.Snapshot()
	hc.mu.RLock()
	counts := make(map[string]struct{ healthy, enabled, total int }, len(snap))
	for model, urls := range snap {
		c := counts[model]
		for _, u := range urls {
			c.total++
			bh, ok := hc.states[u.String()]
			if !ok || bh.State != StateDisabled {
				c.enabled++
			}
			if ok && bh.State == StateHealthy {
				c.healthy++
			}
		}
		counts[model] = c
	}
	hc.mu.RUnlock()
	hc.Metrics.SetModelBackends(counts)
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
	// Drop state entries for URLs that have disappeared from the source,
	// clearing their metric series too so a stale down=1 doesn't linger.
	for k := range hc.states {
		if _, still := urls[k]; !still {
			delete(hc.states, k)
			hc.Metrics.DeleteBackend(k)
		}
	}
	hc.mu.Unlock()

	for k, u := range urls {
		if hc.Disabled != nil && hc.Disabled.IsDisabled(k) {
			hc.record(u, StateDisabled, "", nil)
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
