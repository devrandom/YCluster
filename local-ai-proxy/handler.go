package main

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
)

// hopByHop headers must not be forwarded by a proxy (RFC 7230 §6.1).
var hopByHop = map[string]struct{}{
	"Connection":          {},
	"Keep-Alive":          {},
	"Proxy-Authenticate":  {},
	"Proxy-Authorization": {},
	"Te":                  {},
	"Trailer":             {},
	"Transfer-Encoding":   {},
	"Upgrade":             {},
}

// Handler forwards requests to the backend chosen by Router. The inbound
// request's context is threaded into the upstream call so that a client
// disconnect cancels the upstream request.
type Handler struct {
	router Router
	client *http.Client
	logger *slog.Logger
}

func NewHandler(router Router) *Handler {
	return &Handler{
		router: router,
		client: &http.Client{
			// No Timeout — cancellation is driven by request context.
		},
		logger: slog.Default(),
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	modelRouted := h.router.Models() != nil

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

	bodyReader := r.Body
	if substituteBody != nil {
		bodyReader = io.NopCloser(substituteBody)
	}

	target := *backend
	target.Path = joinPath(target.Path, r.URL.Path)
	target.RawQuery = r.URL.RawQuery

	upstream, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), bodyReader)
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
