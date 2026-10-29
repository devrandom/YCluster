package main

import (
	"errors"
	"net/http"
	"net/url"
)

// serveHealthz renders the operator health endpoint. With no HealthChecker
// configured (passthrough mode, or checks disabled) it still answers with a
// minimal "proxy itself is up" payload.
func (h *Handler) serveHealthz(w http.ResponseWriter, r *http.Request) {
	auth := h.authStatus(r)
	if h.Health == nil {
		writeHealthzNoop(w, r, auth)
		return
	}
	var models map[string][]*url.URL
	if src := h.Health.source; src != nil {
		models = src.Snapshot()
	}
	writeHealthz(w, r, h.Health.Snapshot(), models, auth)
}

// serveModelsList renders /v1/models, filtered to the caller's ACL scope.
func (h *Handler) serveModelsList(w http.ResponseWriter, r *http.Request) {
	models := h.servableModels()
	if acl := h.currentACL(); acl != nil {
		models = filterModelsByACL(models, acl, r)
	}
	writeModelsList(w, models)
}

// rewriteAPIPath normalizes endpoint aliases to the backend's native path.
// llama.cpp and vLLM expose tokenizer endpoints at bare /tokenize and
// /detokenize (neither serves them under /v1/); MinerU's mineru-api serves
// /file_parse without the /v1/ prefix. External clients always use /v1/.
func rewriteAPIPath(r *http.Request) {
	switch r.URL.Path {
	case "/v1/tokenize":
		r.URL.Path = "/tokenize"
	case "/v1/detokenize":
		r.URL.Path = "/detokenize"
	case "/v1/file_parse":
		r.URL.Path = "/file_parse"
	}
}

// handleRouteError writes the appropriate OpenAI error for a routing failure
// and reports whether the request was handled (true) or should continue.
func (h *Handler) handleRouteError(w http.ResponseWriter, r *http.Request, err error) bool {
	if err == nil {
		return false
	}

	h.Metrics.ObserveRouteError(classifyRouteError(err))
	var superseded *SupersededError
	if errors.As(err, &superseded) {
		h.logger.Info("superseded model requested",
			"model", superseded.Model,
			"superseded_by", superseded.SupersededBy,
			"method", r.Method, "path", r.URL.Path)
		writeOpenAIErrorDetail(w, http.StatusNotFound, "invalid_request_error", "model_not_found", err.Error())
		return true
	}
	if errors.Is(err, ErrNoHealthyBackend) {
		h.logger.Warn("no healthy backend", "err", err.Error(), "method", r.Method, "path", r.URL.Path)
		writeOpenAIError(w, http.StatusServiceUnavailable, "api_error", err.Error())
		return true
	}
	h.logger.Info("route rejected", "err", err.Error(), "method", r.Method, "path", r.URL.Path)
	writeOpenAIError(w, http.StatusBadRequest, "invalid_request_error", err.Error())
	return true
}

// requestACL checks the caller's ACL scope against the routed model and
// writes a 403 when denied. Returns true to proceed with dispatch.
func (h *Handler) requestACL(w http.ResponseWriter, r *http.Request, model string) bool {
	acl := h.currentACL()
	if acl == nil {
		return true
	}
	user := r.Header.Get("X-User-Id")
	groups := SplitGroups(r.Header.Get("X-User-Groups"))
	if err := acl.Check(model, user, groups); err != nil {
		h.Metrics.ObserveRouteError(RouteErrACLDenied)
		h.logger.Info("acl denied",
			"model", model, "user", user, "groups", groups,
			"method", r.Method, "path", r.URL.Path)
		writeOpenAIError(w, http.StatusForbidden, "permission_denied", err.Error())
		return false
	}
	return true
}

// filterModelsByACL returns only the model names the caller may access.
func filterModelsByACL(models []string, acl *ACLConfig, r *http.Request) []string {
	user := r.Header.Get("X-User-Id")
	groups := SplitGroups(r.Header.Get("X-User-Groups"))
	filtered := models[:0]
	for _, m := range models {
		if acl.Check(m, user, groups) == nil {
			filtered = append(filtered, m)
		}
	}
	return filtered
}
