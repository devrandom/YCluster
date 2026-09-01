package main

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRequestACLDenialWrites403(t *testing.T) {
	h := &Handler{
		logger:  slog.Default(),
		Metrics: NewMetrics(),
		ACL: &ACLConfig{Models: map[string]ACLModelRule{
			"m": {Entries: []ACLEntry{{Subject: "user:*", Decision: ACLDeny}}},
		}},
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	req.Header.Set("X-User-Id", "bob")

	if h.requestACL(rec, req, "m") {
		t.Fatal("requestACL should deny the request")
	}
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d; want %d", rec.Code, http.StatusForbidden)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("Content-Type = %q; want application/json", ct)
	}
	if !strings.Contains(rec.Body.String(), "permission_denied") {
		t.Fatalf("body missing permission_denied: %q", rec.Body.String())
	}
}
