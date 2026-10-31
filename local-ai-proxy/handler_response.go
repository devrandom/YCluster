package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// modelStateFromBackends classifies a model's rollup state from its
// backends' health. A healthy backend only counts when it also reports
// serving this model, so a backend that is up but has no ready instance
// of the model does not mask the outage. Matches the categories used by
// /healthz: healthy, unavailable, disabled, unknown.
func modelStateFromBackends(model string, urls []*url.URL, snap map[string]BackendHealth) string {
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
		if bh.State == StateHealthy && backendServesModel(bh, model) {
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

func retryReasonForStatus(status int) string {
	switch {
	case status >= 500:
		return "http_5xx"
	case status == http.StatusTooManyRequests:
		return "http_429"
	default:
		return "http_404"
	}
}
