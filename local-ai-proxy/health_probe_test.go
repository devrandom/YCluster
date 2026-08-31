package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthCheckerShouldProbe(t *testing.T) {
	var nilChecker *HealthChecker
	if nilChecker.shouldProbe(nil) {
		t.Fatal("nil checker: want false")
	}

	hc := newTestHealthChecker(t, nil)
	if hc.shouldProbe(nil) {
		t.Fatal("nil URL: want false")
	}

	target := httptest.NewServer(http.NotFoundHandler())
	defer target.Close()
	u := mustURL(t, target.URL)
	if !hc.shouldProbe(u) {
		t.Fatal("ordinary URL: want true")
	}

	hc.Disabled = &fakeDisabled{disabled: map[string]bool{u.String(): true}}
	if hc.shouldProbe(u) {
		t.Fatal("disabled URL: want false")
	}
}

func TestHealthCheckerShouldProbeWhenShuttingDown(t *testing.T) {
	hc := newTestHealthChecker(t, nil)
	target := httptest.NewServer(http.NotFoundHandler())
	defer target.Close()
	u := mustURL(t, target.URL)

	hc.done = make(chan struct{})
	close(hc.done)
	if hc.shouldProbe(u) {
		t.Fatal("stopped checker: want false")
	}
}
