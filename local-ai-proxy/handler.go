package main

import (
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
			writeModelsList(w, h.router.Models())
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

	backend, substituteBody, err := h.router.Route(r)
	if err != nil {
		h.logger.Info("route rejected", "err", err.Error(), "method", r.Method, "path", r.URL.Path)
		writeOpenAIError(w, http.StatusBadRequest, "invalid_request_error", err.Error())
		return
	}

	// Use the substitute body (if ModelRouter consumed the original)
	// directly — not NopCloser-wrapped — so Go's http client detects
	// *bytes.Reader and sets req.ContentLength. Without this, the
	// body goes out chunked, which some backends reject with EOF.
	var upstreamBody io.Reader
	if substituteBody != nil {
		upstreamBody = substituteBody
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
		return
	}
	copyHeaders(upstream.Header, r.Header)

	resp, err := h.client.Do(upstream)
	if err != nil {
		if r.Context().Err() != nil {
			// Client went away; nothing useful to say back.
			return
		}
		h.logger.Warn("upstream request failed", "err", err.Error(), "backend", backend.String())
		writeOpenAIError(w, http.StatusBadGateway, "api_error", "upstream backend unreachable")
		return
	}
	defer resp.Body.Close()

	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	_ = streamBody(w, resp.Body)
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
		anyHealthy := false
		allDisabled := len(urls) > 0
		anyState := false
		for _, u := range urls {
			s := u.String()
			urlStrs = append(urlStrs, s)
			bh, seen := snap[s]
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
		state := "unknown"
		switch {
		case !anyState:
			state = "unknown"
		case anyHealthy:
			state = "healthy"
			modelHealthy++
		case allDisabled:
			state = "disabled"
			modelDisabled++
		default:
			state = "unavailable"
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
