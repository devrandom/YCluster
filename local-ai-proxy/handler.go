package main

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
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

// Handler proxies every inbound request to a single upstream backend.
// The inbound request's context is threaded into the upstream request so
// that a client disconnect cancels the upstream call.
type Handler struct {
	backend *url.URL
	client  *http.Client
	logger  *slog.Logger
}

func NewHandler(backend *url.URL) *Handler {
	return &Handler{
		backend: backend,
		client: &http.Client{
			// No Timeout — cancellation is driven by request context.
		},
		logger: slog.Default(),
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	target := *h.backend
	target.Path = joinPath(target.Path, r.URL.Path)
	target.RawQuery = r.URL.RawQuery

	upstream, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), r.Body)
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
		h.logger.Warn("upstream request failed", "err", err.Error(), "backend", h.backend.String())
		writeOpenAIError(w, http.StatusBadGateway, "api_error", "upstream backend unreachable")
		return
	}
	defer resp.Body.Close()

	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	_ = streamBody(w, resp.Body)
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
