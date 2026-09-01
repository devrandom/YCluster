package main

import (
	"encoding/json"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestHealthzBackendAndModelEntries(t *testing.T) {
	backendURL := mustURL(t, "http://backend.example:8000")
	now := time.Date(2026, 8, 30, 10, 0, 0, 0, time.UTC)
	snap := map[string]BackendHealth{
		backendURL.String(): {State: StateHealthy, LastCheck: now, Models: []string{"alpha"}},
	}
	models := map[string][]*url.URL{"alpha": {backendURL}}

	backends, healthy, down, disabled := healthzBackendEntries(snap)
	if healthy != 1 || down != 0 || disabled != 0 {
		t.Fatalf("unexpected backend counts: healthy=%d down=%d disabled=%d", healthy, down, disabled)
	}
	if len(backends) != 1 || backends[0].URL != backendURL.String() {
		t.Fatalf("unexpected backend rows: %#v", backends)
	}

	modelEntries, modelHealthy, modelDisabled, modelUnavailable := healthzModelEntries(models, snap)
	if modelHealthy != 1 || modelDisabled != 0 || modelUnavailable != 0 {
		t.Fatalf("unexpected model counts: healthy=%d disabled=%d unavailable=%d", modelHealthy, modelDisabled, modelUnavailable)
	}
	if len(modelEntries) != 1 || modelEntries[0].State != "healthy" {
		t.Fatalf("unexpected model rows: %#v", modelEntries)
	}
}

func TestHealthzEntryBranches(t *testing.T) {
	now := time.Date(2026, 8, 30, 10, 0, 0, 0, time.UTC)
	one := mustURL(t, "http://one:8000")
	two := mustURL(t, "http://two:8000")
	snap := map[string]BackendHealth{
		one.String(): {State: StateDown, LastCheck: now, Err: "boom"},
		two.String(): {State: StateDisabled, LastCheck: now},
	}
	backends, healthy, down, disabled := healthzBackendEntries(snap)
	if healthy != 0 || down != 1 || disabled != 1 {
		t.Fatalf("unexpected counts: healthy=%d down=%d disabled=%d", healthy, down, disabled)
	}
	if backends[0].Err != "boom" {
		t.Fatalf("expected backend error to be copied, got %#v", backends[0])
	}

	models := map[string][]*url.URL{
		"empty": {},
		"bad":   {one},
		"off":   {two},
	}
	snap2 := map[string]BackendHealth{two.String(): {State: StateDisabled, LastCheck: now}}
	entries, _, modelDisabled, unavailable := healthzModelEntries(models, snap2)
	if len(entries) != 3 || modelDisabled != 1 || unavailable != 0 {
		t.Fatalf("unexpected model rollups: entries=%#v disabled=%d unavailable=%d", entries, modelDisabled, unavailable)
	}
}

func TestHealthzOverall(t *testing.T) {
	cases := map[string]struct {
		models      []healthzModel
		healthy     int
		disabled    int
		unavailable int
		want        string
	}{
		"empty":  {want: "unknown"},
		"noData": {models: []healthzModel{{Name: "m"}}, want: "unknown"},
		"allDisabled": {
			models: []healthzModel{{Name: "m", State: "disabled"}},
			want:   "unknown",
		},
		"down": {
			models:      []healthzModel{{Name: "m", State: "unavailable"}},
			unavailable: 1,
			want:        "down",
		},
		"degraded": {
			models:      []healthzModel{{Name: "m", State: "unavailable"}, {Name: "n", State: "healthy"}},
			healthy:     1,
			unavailable: 1,
			want:        "degraded",
		},
		"ok": {
			models:  []healthzModel{{Name: "m", State: "healthy"}},
			healthy: 1,
			want:    "ok",
		},
	}
	for name, tc := range cases {
		if got := healthzOverall(tc.models, tc.healthy, tc.unavailable); got != tc.want {
			t.Errorf("%s: got %q, want %q", name, got, tc.want)
		}
	}
}

func TestWriteHealthzAuthDown(t *testing.T) {
	recorder := httptest.NewRecorder()
	auth := &authState{Status: "down"}
	writeHealthz(recorder, httptest.NewRequest("GET", "/healthz", nil), map[string]BackendHealth{}, map[string][]*url.URL{}, auth)

	if recorder.Code != 503 {
		t.Fatalf("status = %d, want 503", recorder.Code)
	}
	var response struct {
		Status string     `json:"status"`
		Auth   *authState `json:"auth"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Status != "down" || response.Auth == nil || response.Auth.Status != "down" {
		t.Fatalf("unexpected response: %#v", response)
	}
}
