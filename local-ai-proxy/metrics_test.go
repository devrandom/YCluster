package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMetricsHandlerNilReceiver(t *testing.T) {
	var m *Metrics
	h := m.Handler()
	if h == nil {
		t.Fatal("expected non-nil handler for nil Metrics")
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusNotFound {
		t.Errorf("expected 404 for nil Metrics, got %d", rec.Code)
	}
}

func TestMetricsHandlerServesRegistry(t *testing.T) {
	m := NewMetrics()
	m.ObserveAttempt("m1", "http://b1", 200, true, 0.5)
	h := m.Handler()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"proxy_requests_total", "backend", "model"} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics body missing %q", want)
		}
	}
}

func TestObserveAttemptTransportError(t *testing.T) {
	m := NewMetrics()
	// status 0 == transport error; must not panic and must count requests.
	m.ObserveAttempt("m1", "http://b1", 0, false, 0.1)
}
