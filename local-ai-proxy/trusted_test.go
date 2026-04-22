package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func runThroughTrusted(t *testing.T, cidrs []string, remoteAddr, xUser string) (gotUser string) {
	t.Helper()
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUser = r.Header.Get("X-User-Id")
		w.WriteHeader(http.StatusOK)
	})
	chained, err := TrustedHeadersMiddleware(cidrs, inner)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.RemoteAddr = remoteAddr
	if xUser != "" {
		req.Header.Set("X-User-Id", xUser)
	}
	rec := httptest.NewRecorder()
	chained.ServeHTTP(rec, req)
	return
}

func TestTrustedHeadersLoopbackTrusted(t *testing.T) {
	for _, addr := range []string{"127.0.0.1:1234", "[::1]:1234"} {
		got := runThroughTrusted(t, nil, addr, "alice")
		if got != "alice" {
			t.Errorf("%s: X-User-Id = %q; want preserved", addr, got)
		}
	}
}

func TestTrustedHeadersExternalStripped(t *testing.T) {
	got := runThroughTrusted(t, nil, "10.0.0.42:55555", "alice")
	if got != "" {
		t.Errorf("X-User-Id = %q; want stripped for external client", got)
	}
}

func TestTrustedHeadersCustomCIDR(t *testing.T) {
	cidrs := []string{"10.0.0.0/8"}

	if got := runThroughTrusted(t, cidrs, "10.0.0.42:55555", "alice"); got != "alice" {
		t.Errorf("10.0.x.x: got %q; want alice", got)
	}
	if got := runThroughTrusted(t, cidrs, "127.0.0.1:1234", "alice"); got != "" {
		t.Errorf("loopback with custom CIDR: got %q; want stripped", got)
	}
	if got := runThroughTrusted(t, cidrs, "192.168.1.1:1234", "alice"); got != "" {
		t.Errorf("non-matching IP: got %q; want stripped", got)
	}
}

func TestTrustedHeadersMalformedRemoteAddr(t *testing.T) {
	// Ensure we don't panic on weird RemoteAddr values.
	got := runThroughTrusted(t, nil, "garbage", "alice")
	if got != "" {
		t.Errorf("malformed RemoteAddr: got %q; want stripped", got)
	}
}

func TestTrustedHeadersInvalidCIDRErrors(t *testing.T) {
	_, err := TrustedHeadersMiddleware([]string{"not-a-cidr"}, http.NotFoundHandler())
	if err == nil || !strings.Contains(err.Error(), "not-a-cidr") {
		t.Errorf("want parse error, got %v", err)
	}
}

func TestTrustedHeadersMissingUserPassesUnchanged(t *testing.T) {
	// Even with trust, no X-User-Id means nothing is added.
	got := runThroughTrusted(t, nil, "127.0.0.1:1234", "")
	if got != "" {
		t.Errorf("got %q; want empty", got)
	}
}
