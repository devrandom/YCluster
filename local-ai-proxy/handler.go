package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// hopByHop headers must not be forwarded by a proxy (RFC 7230 §6.1).
// Content-Length is also skipped: Go's http client sets it from
// req.ContentLength, and copying the client's header causes duplicate
// or conflicting values when we've substituted the body.
var hopByHop = map[string]struct{}{
	"Connection":          {},
	"Keep-Alive":          {},
	"Proxy-Authenticate":  {},
	"Proxy-Authorization": {},
	"Te":                  {},
	"Trailer":             {},
	"Transfer-Encoding":   {},
	"Upgrade":             {},
	"Content-Length":      {},
}

// Handler forwards requests to the backend chosen by Router. The inbound
// request's context is threaded into the upstream call so that a client
// disconnect cancels the upstream request.
type Handler struct {
	router Router
	client *http.Client
	logger *slog.Logger

	// Health is optional. When set, GET /healthz returns a JSON summary
	// of per-backend state.
	Health *HealthChecker

	// Load, if set, is incremented around each upstream call so the
	// router can pick the least-loaded backend.
	Load LoadTracker

	// Metrics, if set, receives Prometheus observations from the
	// request path. Nil-safe.
	Metrics *Metrics
}

func NewHandler(router Router) *Handler {
	// Disable HTTP keep-alives to upstream. LLM requests are long
	// (100ms–minutes) so the ~1ms saved by connection reuse is
	// negligible, while the default pool causes occasional POST
	// failures with "EOF" when a backend silently closes an idle
	// connection (Go's Transport can't retry non-idempotent POSTs).
	// Observed against mlx-server and llama.cpp setups.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DisableKeepAlives = true
	return &Handler{
		router: router,
		client: &http.Client{
			Transport: transport,
			// No Timeout — cancellation is driven by request context.
		},
		logger: slog.Default(),
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	modelRouted := h.router.Models() != nil

	// Operator endpoint: current backend health states + per-model rollup.
	if r.Method == http.MethodGet && r.URL.Path == "/healthz" {
		if h.Health != nil {
			var models map[string][]*url.URL
			if src := h.Health.source; src != nil {
				models = src.Snapshot()
			}
			writeHealthz(w, h.Health.Snapshot(), models)
			return
		}
		writeHealthzNoop(w)
		return
	}

	// For multi-backend routers, we own the /v1/models response.
	// Passthrough (Models()==nil) falls through to proxy the upstream.
	if r.Method == http.MethodGet && r.URL.Path == "/v1/models" {
		if modelRouted {
			writeModelsList(w, h.servableModels())
			return
		}
	}

	// In model-routed mode, reject obvious non-API paths with 404
	// rather than handing them off to the body parser. Any path under
	// /v1/ is treated as a potential OpenAI-compatible endpoint — the
	// backend decides whether that specific endpoint actually exists,
	// so we avoid enumerating a list that would bit-rot as OpenAI and
	// backends add endpoints.
	if modelRouted && !strings.HasPrefix(r.URL.Path, "/v1/") {
		writeOpenAIError(w, http.StatusNotFound, "not_found_error",
			"unknown endpoint: "+r.URL.Path)
		return
	}

	route, err := h.router.Route(r)
	if err != nil {
		h.Metrics.ObserveRouteError(classifyRouteError(err))
		if errors.Is(err, ErrNoHealthyBackend) {
			h.logger.Warn("no healthy backend", "err", err.Error(), "method", r.Method, "path", r.URL.Path)
			writeOpenAIError(w, http.StatusServiceUnavailable, "api_error", err.Error())
			return
		}
		h.logger.Info("route rejected", "err", err.Error(), "method", r.Method, "path", r.URL.Path)
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request_error", err.Error())
		return
	}

	h.dispatch(w, r, route)
}

// classifyRouteError maps a Route() error to a stable metric label.
func classifyRouteError(err error) string {
	if errors.Is(err, ErrNoHealthyBackend) {
		return RouteErrNoHealthy
	}
	msg := err.Error()
	switch {
	case strings.HasPrefix(msg, "unknown model"):
		return RouteErrUnknownModel
	case strings.Contains(msg, "too large"):
		return RouteErrBodyTooLarge
	default:
		return RouteErrInvalidRequest
	}
}

// dispatch tries each eligible backend in load-aware order. On the
// last remaining candidate (or when the body isn't replayable — i.e.
// passthrough single-backend mode), whatever response comes back is
// committed to the client verbatim. Earlier attempts can be retried
// if they transport-error or return any 4xx/5xx: we treat fan-out
// backends as interchangeable, so a 4xx from one backend (config
// drift: missing model, stricter template, per-backend auth misconfig)
// shouldn't reach the client if a peer would serve it.
//
// Every retryable failure fires a Probe at that backend so the health
// checker re-verifies out-of-band. A 4xx is a "the backend answered";
// the probe decides if that backend stays routable.
func (h *Handler) dispatch(w http.ResponseWriter, r *http.Request, route *RouteResult) {
	remaining := route.Candidates
	for len(remaining) > 0 {
		if r.Context().Err() != nil {
			return
		}
		backend := PickBackend(remaining, h.loadRead())
		remaining = removeURL(remaining, backend)
		lastTry := len(remaining) == 0 || route.Body == nil

		if done := h.tryOnce(w, r, route.Model, backend, route.Body, lastTry); done {
			return
		}
	}
}

// tryOnce issues one upstream request. Returns done=true if the
// response was committed to the client or if we emitted a terminal
// error; done=false signals "try the next candidate". When lastTry
// is true the response (success or failure) is committed verbatim.
func (h *Handler) tryOnce(w http.ResponseWriter, r *http.Request, model string, backend *url.URL, bodyBytes []byte, lastTry bool) bool {
	backendStr := backend.String()
	start := time.Now()
	if h.Load != nil {
		h.Load.Inc(backendStr)
		h.Metrics.SetInflight(backendStr, h.Load.Count(backendStr))
		defer func() {
			h.Load.Dec(backendStr)
			h.Metrics.SetInflight(backendStr, h.Load.Count(backendStr))
		}()
	}

	// Use *bytes.Reader (not NopCloser) so Go's http client detects the
	// type and sets req.ContentLength. Without this, the body goes out
	// chunked, which some backends reject with EOF.
	var upstreamBody io.Reader
	if bodyBytes != nil {
		upstreamBody = bytes.NewReader(bodyBytes)
	} else {
		upstreamBody = r.Body
	}

	target := *backend
	target.Path = joinPath(target.Path, r.URL.Path)
	target.RawQuery = r.URL.RawQuery

	upstream, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), upstreamBody)
	if err != nil {
		h.logger.Warn("build upstream request failed", "err", err.Error())
		writeOpenAIError(w, http.StatusInternalServerError, "api_error", "failed to build upstream request")
		return true
	}
	copyHeaders(upstream.Header, r.Header)

	resp, err := h.client.Do(upstream)
	if err != nil {
		if r.Context().Err() != nil {
			return true // client went away
		}
		h.probe(backend)
		h.Metrics.ObserveAttempt(model, backendStr, 0, false, 0)
		if lastTry {
			h.logger.Warn("upstream transport error", "err", err.Error(), "backend", backendStr)
			writeOpenAIError(w, http.StatusBadGateway, "api_error", "upstream backend unreachable")
			return true
		}
		h.logger.Info("upstream transport error, retrying", "err", err.Error(), "backend", backendStr)
		h.Metrics.ObserveRetry(backendStr, "transport_error")
		return false
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 && !lastTry {
		h.probe(backend)
		h.Metrics.ObserveAttempt(model, backendStr, resp.StatusCode, false, 0)
		h.Metrics.ObserveRetry(backendStr, retryReasonForStatus(resp.StatusCode))
		h.logger.Info("upstream error status, retrying", "status", resp.StatusCode, "backend", backendStr)
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<12))
		return false
	}

	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	_ = streamBody(w, resp.Body)
	committed := resp.StatusCode < 400
	h.Metrics.ObserveAttempt(model, backendStr, resp.StatusCode, committed, time.Since(start).Seconds())
	return true
}

func retryReasonForStatus(status int) string {
	if status >= 500 {
		return "http_5xx"
	}
	return "http_4xx"
}

func (h *Handler) probe(backend *url.URL) {
	if h.Health != nil {
		h.Health.Probe(backend)
	}
}

// loadRead exposes h.Load as the read-only Load interface for router
// re-picks during retry; returns nil if no tracker is wired up.
func (h *Handler) loadRead() Load {
	if h.Load == nil {
		return nil
	}
	return h.Load
}

// removeURL returns s with the first occurrence of u removed. Used by
// dispatch to narrow the retry set as attempts fail.
func removeURL(s []*url.URL, u *url.URL) []*url.URL {
	for i, x := range s {
		if x == u || x.String() == u.String() {
			return append(s[:i:i], s[i+1:]...)
		}
	}
	return s
}

// writeHealthz writes a JSON summary of per-backend and per-model state.
//
// Model state is derived from its backends:
//   - healthy: at least one backend is healthy
//   - disabled: every backend is disabled (operator-acknowledged)
//   - unavailable: has backends, none healthy, not all disabled
//   - unknown: no check data yet
//
// Overall status is a rollup of model state (disabled models don't count
// as alerts): ok | degraded | down | unknown.
func writeHealthz(w http.ResponseWriter, snap map[string]BackendHealth, models map[string][]*url.URL) {
	type entry struct {
		URL       string `json:"url"`
		State     string `json:"state"`
		LastCheck string `json:"last_check"`
		Err       string `json:"err,omitempty"`
	}
	type modelEntry struct {
		Name     string   `json:"name"`
		State    string   `json:"state"`
		Backends []string `json:"backends"`
	}
	urls := make([]string, 0, len(snap))
	for k := range snap {
		urls = append(urls, k)
	}
	sort.Strings(urls)

	backends := make([]entry, 0, len(urls))
	healthy, down, disabled := 0, 0, 0
	for _, u := range urls {
		bh := snap[u]
		switch bh.State {
		case StateHealthy:
			healthy++
		case StateDown:
			down++
		case StateDisabled:
			disabled++
		}
		e := entry{
			URL:       u,
			State:     bh.State.String(),
			LastCheck: bh.LastCheck.UTC().Format(time.RFC3339),
		}
		if bh.Err != "" {
			e.Err = bh.Err
		}
		backends = append(backends, e)
	}

	// Per-model rollup. Model state is derived from its backends.
	modelNames := make([]string, 0, len(models))
	for name := range models {
		modelNames = append(modelNames, name)
	}
	sort.Strings(modelNames)

	modelEntries := make([]modelEntry, 0, len(modelNames))
	modelHealthy, modelDisabled, modelUnavailable := 0, 0, 0
	for _, name := range modelNames {
		urls := models[name]
		urlStrs := make([]string, 0, len(urls))
		for _, u := range urls {
			urlStrs = append(urlStrs, u.String())
		}
		state := modelStateFromBackends(urls, snap)
		switch state {
		case "healthy":
			modelHealthy++
		case "disabled":
			modelDisabled++
		case "unavailable":
			modelUnavailable++
		}
		modelEntries = append(modelEntries, modelEntry{
			Name: name, State: state, Backends: urlStrs,
		})
	}

	// Overall status is a model-level rollup — a down backend that has
	// healthy siblings for its model shouldn't page. Disabled models are
	// operator-acknowledged and don't contribute to degraded/down.
	overall := "ok"
	accountable := modelHealthy + modelUnavailable
	switch {
	case len(modelEntries) == 0:
		overall = "unknown"
	case modelHealthy == 0 && modelUnavailable == 0:
		overall = "unknown"
	case modelUnavailable > 0 && modelHealthy == 0 && accountable > 0:
		overall = "down"
	case modelUnavailable > 0:
		overall = "degraded"
	}

	resp := map[string]any{
		"status":   overall,
		"healthy":  healthy,
		"down":     down,
		"disabled": disabled,
		"backends": backends,
		"models":   modelEntries,
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(resp)
}

// writeHealthzNoop responds when a Handler has no HealthChecker
// (passthrough mode, or checks disabled). Proxy-itself-up is 200.
func writeHealthzNoop(w http.ResponseWriter, ) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":  "ok",
		"message": "health checks disabled",
	})
}

// servableModels returns the model names that should appear in
// /v1/models: those with at least one healthy backend, plus those
// whose state is still unknown (no check completed yet). Models that
// are operator-disabled or unavailable (no healthy backend) are
// excluded so clients don't pick a dead route.
func (h *Handler) servableModels() []string {
	all := h.router.Models()
	if h.Health == nil || h.Health.source == nil {
		return all
	}
	models := h.Health.source.Snapshot()
	snap := h.Health.Snapshot()
	out := make([]string, 0, len(all))
	for _, name := range all {
		if modelStateFromBackends(models[name], snap) == "healthy" {
			out = append(out, name)
			continue
		}
		// Also include unknown — we lack signal, let the client try.
		if modelStateFromBackends(models[name], snap) == "unknown" {
			out = append(out, name)
		}
	}
	return out
}

// modelStateFromBackends classifies a model's rollup state from its
// backends' health. Matches the categories used by /healthz:
// healthy, unavailable, disabled, unknown.
func modelStateFromBackends(urls []*url.URL, snap map[string]BackendHealth) string {
	if len(urls) == 0 {
		return "unknown"
	}
	anyHealthy, anyState, allDisabled := false, false, true
	for _, u := range urls {
		bh, seen := snap[u.String()]
		if !seen {
			allDisabled = false
			continue
		}
		anyState = true
		if bh.State != StateDisabled {
			allDisabled = false
		}
		if bh.State == StateHealthy {
			anyHealthy = true
		}
	}
	switch {
	case !anyState:
		return "unknown"
	case anyHealthy:
		return "healthy"
	case allDisabled:
		return "disabled"
	default:
		return "unavailable"
	}
}

// writeModelsList emits an OpenAI-compatible /v1/models response built
// from the router's known model names.
func writeModelsList(w http.ResponseWriter, models []string) {
	type modelEntry struct {
		ID      string `json:"id"`
		Object  string `json:"object"`
		OwnedBy string `json:"owned_by"`
	}
	data := make([]modelEntry, 0, len(models))
	for _, m := range models {
		data = append(data, modelEntry{ID: m, Object: "model", OwnedBy: "local-ai-proxy"})
	}
	resp := struct {
		Object string       `json:"object"`
		Data   []modelEntry `json:"data"`
	}{Object: "list", Data: data}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(resp)
}

// streamBody copies upstream → client, flushing after each read so that
// SSE / chunked responses reach the client promptly. A write error aborts
// the copy; the caller's request context is already wired to cancel the
// upstream connection in that case.
func streamBody(w http.ResponseWriter, body io.Reader) error {
	rc := http.NewResponseController(w)
	buf := make([]byte, 32*1024)
	for {
		n, rerr := body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return werr
			}
			if ferr := rc.Flush(); ferr != nil && !errors.Is(ferr, http.ErrNotSupported) {
				return ferr
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				return nil
			}
			return rerr
		}
	}
}

func copyHeaders(dst, src http.Header) {
	for k, vv := range src {
		if _, skip := hopByHop[http.CanonicalHeaderKey(k)]; skip {
			continue
		}
		dst[k] = append(dst[k], vv...)
	}
}

func joinPath(a, b string) string {
	switch {
	case a == "":
		return b
	case b == "":
		return a
	case strings.HasSuffix(a, "/") && strings.HasPrefix(b, "/"):
		return a + b[1:]
	case !strings.HasSuffix(a, "/") && !strings.HasPrefix(b, "/"):
		return a + "/" + b
	default:
		return a + b
	}
}
