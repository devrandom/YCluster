package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// TestServableModelsExcludesDisabledAndUnavailable verifies that
// /v1/models returns only models with at least one healthy backend
// (or no health data yet — "unknown" is included).
func TestServableModelsExcludesDisabledAndUnavailable(t *testing.T) {
	good := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer good.Close()
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close()

	goodURL := mustURL(t, good.URL)
	deadURL := mustURL(t, dead.URL)
	disabledURL := mustURL(t, "http://disabled.example:8000")

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {goodURL},     // healthy
		"beta":  {deadURL},     // unavailable (backend dead, not disabled)
		"gamma": {disabledURL}, // disabled
	}}
	hc := NewHealthChecker(src, 10*time.Millisecond, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	hc.Disabled = &fakeDisabled{
		disabled: map[string]bool{disabledURL.String(): true},
	}
	hc.checkAll(context.Background())

	h := NewHandler(NewModelRouter(src))
	h.Health = hc
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}

	ids := make([]string, 0, len(body.Data))
	for _, d := range body.Data {
		ids = append(ids, d.ID)
	}
	got := strings.Join(ids, ",")

	if !strings.Contains(got, "alpha") {
		t.Errorf("alpha (healthy) missing from /v1/models: %v", ids)
	}
	if strings.Contains(got, "beta") {
		t.Errorf("beta (unavailable) should not appear in /v1/models: %v", ids)
	}
	if strings.Contains(got, "gamma") {
		t.Errorf("gamma (disabled) should not appear in /v1/models: %v", ids)
	}
}

// TestServableModelsIncludesUnknown — before a health check has run,
// models should still be listed (we lack signal to exclude).
func TestServableModelsIncludesUnknown(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://never-checked:1")},
	}}
	// Construct HealthChecker but do NOT run checkAll.
	hc := NewHealthChecker(src, time.Hour, slog.New(slog.NewJSONHandler(io.Discard, nil)))

	h := NewHandler(NewModelRouter(src))
	h.Health = hc
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, _ := http.Get(srv.URL + "/v1/models")
	defer resp.Body.Close()
	var body struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	if len(body.Data) != 1 || body.Data[0].ID != "alpha" {
		t.Errorf("alpha (unknown state) should be listed: %v", body.Data)
	}
}

// TestServableModelsWithoutHealthShowsAll — handler with no Health
// checker (passthrough-ish modes, or health disabled) should still
// list everything the router knows.
func TestServableModelsWithoutHealthShowsAll(t *testing.T) {
	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, "http://h1:1")},
		"beta":  {mustURL(t, "http://h2:1")},
	}}
	h := NewHandler(NewModelRouter(src))
	// h.Health intentionally nil
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, _ := http.Get(srv.URL + "/v1/models")
	defer resp.Body.Close()
	var body struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	if len(body.Data) != 2 {
		t.Errorf("want 2 models (no health info), got %v", body.Data)
	}
}
