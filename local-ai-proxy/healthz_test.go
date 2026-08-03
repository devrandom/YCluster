package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestHealthzReportsPerBackendState(t *testing.T) {
	good := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer good.Close()
	bad := httptest.NewServer(http.NotFoundHandler())
	bad.Close()

	src := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, good.URL)},
		"beta":  {mustURL(t, bad.URL)},
	}}
	hc := NewHealthChecker(src, 10*time.Millisecond, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	hc.checkAll(context.Background())

	h := NewHandler(NewModelRouter(src))
	h.Health = hc
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}

	var body struct {
		Status   string `json:"status"`
		Healthy  int    `json:"healthy"`
		Down     int    `json:"down"`
		Backends []struct {
			URL   string `json:"url"`
			State string `json:"state"`
			Err   string `json:"err,omitempty"`
		} `json:"backends"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Status != "degraded" {
		t.Errorf("status = %q; want degraded (one healthy, one down)", body.Status)
	}
	if body.Healthy != 1 || body.Down != 1 {
		t.Errorf("counts healthy=%d down=%d; want 1/1", body.Healthy, body.Down)
	}
	if len(body.Backends) != 2 {
		t.Fatalf("backends len = %d", len(body.Backends))
	}
	// Sorted by URL — bad (.Close()'d, port lower) may or may not come
	// first depending on allocation. Just check both are present.
	states := map[string]string{}
	for _, b := range body.Backends {
		states[b.URL] = b.State
	}
	if states[good.URL] != "healthy" {
		t.Errorf("good backend state = %q", states[good.URL])
	}
	if states[bad.URL] != "down" {
		t.Errorf("bad backend state = %q", states[bad.URL])
	}
}

func TestHealthzWithoutCheckerReportsOK(t *testing.T) {
	h := NewHandler(NewPassthroughRouter(mustURL(t, "http://x:1")))
	// No Health set.
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	if body["status"] != "ok" {
		t.Errorf("status = %v; want ok", body["status"])
	}
}

func TestHealthzFoldsAuthStatus(t *testing.T) {
	authUp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer authUp.Close()
	authDown := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer authDown.Close()

	// Exercised against both the noop path (no Health) and the
	// Health-set path, so the auth fold is covered in each writer.
	run := func(name, authURL string, wantStatus int, wantOverall string, withHealth bool) {
		t.Run(name, func(t *testing.T) {
			var h *Handler
			if withHealth {
				src := &fakeSource{m: map[string][]*url.URL{
					"alpha": {mustURL(t, authUp.URL)},
				}}
				hc := NewHealthChecker(src, time.Hour, slog.New(slog.NewJSONHandler(io.Discard, nil)))
				hc.checkAll(context.Background())
				h = NewHandler(NewModelRouter(src))
				h.Health = hc
			} else {
				h = NewHandler(NewPassthroughRouter(mustURL(t, "http://x:1")))
			}
			h.AuthHealthURL = authURL
			srv := httptest.NewServer(h)
			defer srv.Close()

			resp, err := http.Get(srv.URL + "/healthz")
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != wantStatus {
				t.Fatalf("status = %d; want %d", resp.StatusCode, wantStatus)
			}
			var body struct {
				Status string `json:"status"`
				Auth   struct {
					Status string `json:"status"`
				} `json:"auth"`
			}
			if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if body.Status != wantOverall {
				t.Errorf("status = %q; want %q", body.Status, wantOverall)
			}
		})
	}

	for _, withHealth := range []bool{false, true} {
		kind := "noop"
		if withHealth {
			kind = "health"
		}
		run(kind+"-auth-up-200", authUp.URL, http.StatusOK, "ok", withHealth)
		run(kind+"-auth-down-503", authDown.URL, http.StatusServiceUnavailable, "down", withHealth)
	}
}
