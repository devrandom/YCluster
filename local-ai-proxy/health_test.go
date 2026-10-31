package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// countBackendSeries returns the number of series currently exported for
// the named metric family by the checker's registry.
func countBackendSeries(t *testing.T, m *Metrics, name string) int {
	t.Helper()
	fams, err := m.registry.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}
	for _, f := range fams {
		if f.GetName() == name {
			return len(f.GetMetric())
		}
	}
	return 0
}

func newTestHealthChecker(t *testing.T, m map[string][]*url.URL) *HealthChecker {
	t.Helper()
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	return NewHealthChecker(&fakeSource{m: m}, 10*time.Millisecond, logger)
}

func TestHealthCheckerMarksUpstreamsHealthy(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("upstream got path %q; want /v1/models", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	snap := hc.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("got %d states; want 1", len(snap))
	}
	bh, ok := snap[up.URL]
	if !ok {
		t.Fatalf("no state for %s", up.URL)
	}
	if bh.State != StateHealthy {
		t.Errorf("state = %v; want healthy", bh.State)
	}
	if bh.Err != "" {
		t.Errorf("err = %q; want empty", bh.Err)
	}
	if bh.LastCheck.IsZero() {
		t.Error("LastCheck not set")
	}
}

func TestHealthCheckerMarksUnreachableDown(t *testing.T) {
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close() // guaranteed refuse

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, dead.URL)},
	})
	hc.checkAll(context.Background())

	bh := hc.Snapshot()[dead.URL]
	if bh.State != StateDown {
		t.Errorf("state = %v; want down", bh.State)
	}
	if bh.Err == "" {
		t.Error("Err should describe the failure")
	}
}

func TestHealthChecker4xxCountsAsHealthy(t *testing.T) {
	// Some backends require auth and return 401 to unauthenticated
	// /v1/models. That still means the backend is alive.
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	if s := hc.Snapshot()[up.URL].State; s != StateHealthy {
		t.Errorf("state for 401 = %v; want healthy", s)
	}
}

func TestHealthChecker5xxCountsAsDown(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	if s := hc.Snapshot()[up.URL].State; s != StateDown {
		t.Errorf("state for 503 = %v; want down", s)
	}
}

func TestHealthCheckerTracksStateChanges(t *testing.T) {
	var alive atomic.Bool
	alive.Store(true)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if alive.Load() {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})

	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateHealthy {
		t.Fatal("initial state should be healthy")
	}

	alive.Store(false)
	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateDown {
		t.Fatal("state should have flipped to down")
	}

	alive.Store(true)
	hc.checkAll(context.Background())
	if hc.Snapshot()[up.URL].State != StateHealthy {
		t.Fatal("state should have flipped back to healthy")
	}
}

func TestHealthCheckerDropsStaleEntries(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	source := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	}}
	hc := NewHealthChecker(source, time.Millisecond, slog.New(slog.NewJSONHandler(io.Discard, nil)))

	hc.checkAll(context.Background())
	if _, ok := hc.Snapshot()[up.URL]; !ok {
		t.Fatal("state should exist after first check")
	}

	// Remove the model from the source.
	source.m = map[string][]*url.URL{}
	hc.checkAll(context.Background())
	if _, ok := hc.Snapshot()[up.URL]; ok {
		t.Fatal("stale entry should have been dropped")
	}
}

func TestHealthCheckerClearsMetricsForDroppedBackend(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	source := &fakeSource{m: map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	}}
	hc := NewHealthChecker(source, time.Millisecond, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	hc.Metrics = NewMetrics()

	hc.checkAll(context.Background())
	// One backend, one series per state label (healthy/down/disabled/unknown).
	if n := countBackendSeries(t, hc.Metrics, "local_ai_proxy_backend_state"); n != len(backendStateLabels) {
		t.Fatalf("got %d backend_state series after first check; want %d", n, len(backendStateLabels))
	}

	// Remove the model; the backend's metric series must be cleared, not
	// left frozen at its last value (the m2.xc:8080 ghost-alert bug).
	source.m = map[string][]*url.URL{}
	hc.checkAll(context.Background())
	if n := countBackendSeries(t, hc.Metrics, "local_ai_proxy_backend_state"); n != 0 {
		t.Fatalf("got %d backend_state series after backend removed; want 0", n)
	}
	if n := countBackendSeries(t, hc.Metrics, "local_ai_proxy_backend_healthy"); n != 0 {
		t.Fatalf("got %d backend_healthy series after backend removed; want 0", n)
	}
}

func TestHealthCheckerDeduplicatesURLsAcrossModels(t *testing.T) {
	var hits atomic.Int32
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {u},
		"beta":  {u}, // same URL, different model
		"gamma": {u},
	})
	hc.checkAll(context.Background())

	if hits.Load() != 1 {
		t.Errorf("upstream hit %d times; want 1 (one check per unique URL)", hits.Load())
	}
	if len(hc.Snapshot()) != 1 {
		t.Errorf("got %d state entries; want 1", len(hc.Snapshot()))
	}
}

// TestHealthCheckerServesModelDetectsStaleEntries verifies that when a
// backend's /v1/models response lists specific model IDs, ServesModel
// returns false for a model mapped to that backend that the backend
// doesn't actually serve — the stale etcd-entry case.
func TestHealthCheckerServesModelDetectsStaleEntries(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"alpha"},{"id":"beta"}]}`))
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {u},
		"gamma": {u}, // stale — backend doesn't serve gamma
	})
	hc.checkAll(context.Background())

	bh := hc.Snapshot()[up.URL]
	if bh.State != StateHealthy {
		t.Fatalf("state = %v; want healthy", bh.State)
	}
	if !hc.ServesModel(up.URL, "alpha") {
		t.Error("ServesModel(alpha) = false; want true (backend serves alpha)")
	}
	if hc.ServesModel(up.URL, "gamma") {
		t.Error("ServesModel(gamma) = true; want false (backend doesn't serve gamma)")
	}
}

// TestHealthCheckerServesModelUnknownListAllowsAll verifies that when
// the backend's model list can't be determined (e.g. 401), ServesModel
// returns true for any model so the router doesn't filter out a
// reachable backend we couldn't interrogate.
func TestHealthCheckerServesModelUnknownListAllowsAll(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer up.Close()

	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {mustURL(t, up.URL)},
	})
	hc.checkAll(context.Background())

	if !hc.ServesModel(up.URL, "alpha") {
		t.Error("ServesModel(alpha) = false; want true (model list unknown)")
	}
	if !hc.ServesModel(up.URL, "anything") {
		t.Error("ServesModel(anything) = false; want true (model list unknown)")
	}
}

// exoInstance renders one placed exo instance with the given runner IDs.
func exoInstance(id, model string, runners ...string) string {
	parts := make([]string, len(runners))
	for i, r := range runners {
		parts[i] = fmt.Sprintf(`%q:{}`, r)
	}
	return fmt.Sprintf(
		`%q:{"MlxJacclInstance":{"shardAssignments":{"modelId":%q,"runnerToShard":{%s}}}}`,
		id, model, strings.Join(parts, ","))
}

func exoInstances(entries ...string) string {
	return "{" + strings.Join(entries, ",") + "}"
}

// TestHealthCheckerExoProbeUsesPlacementState covers the exo failure
// mode the /v1/models catalog probe cannot see: the backend answers
// 200 with every model card while no instance is placed, or while its
// runners are still loading.
func TestHealthCheckerExoProbeUsesPlacementState(t *testing.T) {
	const model = "mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8"
	cases := map[string]struct {
		instances       string
		runners         string
		instancesStatus int
		runnersStatus   int
		wantState       BackendState
		wantErr         string
		wantModel       []string
	}{
		"no instance": {
			instances: "{}",
			runners:   "{}",
			wantState: StateDown,
			wantErr:   "no instance placed",
			wantModel: []string{},
		},
		"loading": {
			instances: exoInstances(exoInstance("i1", model, "r1", "r2")),
			runners:   `{"r1":{"RunnerReady":{}},"r2":{"RunnerLoading":{"layersLoaded":10}}}`,
			wantState: StateDown,
			wantErr:   "runners not ready",
			wantModel: []string{},
		},
		"ready": {
			instances: exoInstances(exoInstance("i1", model, "r1", "r2")),
			runners:   `{"r1":{"RunnerReady":{}},"r2":{"RunnerReady":{}}}`,
			wantState: StateHealthy,
			wantModel: []string{model},
		},
		"running": {
			instances: exoInstances(exoInstance("i1", model, "r1", "r2")),
			runners:   `{"r1":{"RunnerRunning":{}},"r2":{"RunnerRunning":{}}}`,
			wantState: StateHealthy,
			wantModel: []string{model},
		},
		"mixed ready and running": {
			instances: exoInstances(exoInstance("i1", model, "r1", "r2")),
			runners:   `{"r1":{"RunnerReady":{}},"r2":{"RunnerRunning":{}}}`,
			wantState: StateHealthy,
			wantModel: []string{model},
		},
		"missing runner": {
			instances: exoInstances(exoInstance("i1", model, "r1", "r2")),
			runners:   `{"r1":{"RunnerReady":{}}}`,
			wantState: StateDown,
			wantErr:   "runners not ready",
			wantModel: []string{},
		},
		"duplicate ready instances": {
			instances: exoInstances(
				exoInstance("i1", model, "r1"),
				exoInstance("i2", model, "r2"),
			),
			runners:   `{"r1":{"RunnerReady":{}},"r2":{"RunnerReady":{}}}`,
			wantState: StateHealthy,
			wantModel: []string{model},
		},
		"instances 404": {
			instancesStatus: http.StatusNotFound,
			wantState:       StateDown,
			wantErr:         "exo instances: HTTP 404",
			wantModel:       []string{},
		},
		"runners 500": {
			instances:     exoInstances(exoInstance("i1", model, "r1")),
			runnersStatus: http.StatusInternalServerError,
			wantState:     StateDown,
			wantErr:       "exo runners: HTTP 500",
			wantModel:     []string{},
		},
		"malformed runners": {
			instances: exoInstances(exoInstance("i1", model, "r1")),
			runners:   "not-json",
			wantState: StateDown,
			wantErr:   "exo runners: parse",
			wantModel: []string{},
		},
		"only ready instances count": {
			instances: exoInstances(
				exoInstance("i1", model, "r1"),
				exoInstance("i2", "other-model", "r2"),
			),
			runners:   `{"r1":{"RunnerReady":{}},"r2":{"RunnerLoading":{}}}`,
			wantState: StateHealthy,
			wantModel: []string{model},
		},
		"unrecognized instances": {
			instances: `{"i1":{"UnknownInstance":{}}}`,
			runners:   `{}`,
			wantState: StateDown,
			wantErr:   "exo instances: unrecognized state",
			wantModel: []string{},
		},
		"malformed instances": {
			instances: "not-json",
			runners:   "{}",
			wantState: StateDown,
			wantErr:   "exo instances: parse",
			wantModel: []string{},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/v1/models":
					_, _ = fmt.Fprintf(w, `{"object":"list","data":[{"id":%q,"owned_by":"exo"}]}`, model)
				case "/state/instances":
					if tc.instancesStatus != 0 {
						w.WriteHeader(tc.instancesStatus)
					}
					_, _ = io.WriteString(w, tc.instances)
				case "/state/runners":
					if tc.runnersStatus != 0 {
						w.WriteHeader(tc.runnersStatus)
					}
					_, _ = io.WriteString(w, tc.runners)
				default:
					t.Errorf("unexpected upstream path %q", r.URL.Path)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer up.Close()

			u := mustURL(t, up.URL)
			hc := newTestHealthChecker(t, map[string][]*url.URL{model: {u}})
			hc.checkAll(context.Background())

			bh := hc.Snapshot()[u.String()]
			if bh.State != tc.wantState {
				t.Fatalf("state = %v (err %q); want %v", bh.State, bh.Err, tc.wantState)
			}
			if !strings.HasPrefix(bh.Err, tc.wantErr) {
				t.Errorf("err = %q; want prefix %q", bh.Err, tc.wantErr)
			}
			if !reflect.DeepEqual(bh.Models, tc.wantModel) {
				t.Errorf("Models = %#v; want %#v", bh.Models, tc.wantModel)
			}
			if tc.wantState == StateDown && hc.ServesModel(u.String(), model) {
				t.Error("ServesModel(down backend) = true; want false")
			}
		})
	}
}

// TestHealthCheckerNonExoBackendKeepsCatalogProbe verifies that a
// backend which does not identify as exo is still checked with the
// plain /v1/models probe and never hits the exo state endpoints.
func TestHealthCheckerNonExoBackendKeepsCatalogProbe(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("unexpected upstream path %q", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = io.WriteString(w, `{"object":"list","data":[{"id":"alpha"}]}`)
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{"alpha": {u}})
	hc.checkAll(context.Background())

	bh := hc.Snapshot()[u.String()]
	if bh.State != StateHealthy {
		t.Fatalf("state = %v; want healthy", bh.State)
	}
	if !reflect.DeepEqual(bh.Models, []string{"alpha"}) {
		t.Errorf("Models = %#v; want [alpha]", bh.Models)
	}
	if hc.ServesModel(u.String(), "beta") {
		t.Error("ServesModel(beta) = true; want false")
	}
}

// TestHealthCheckerModelMetricsIgnoreNonServingBackend verifies that a
// healthy backend which reports not serving a model does not keep that
// model's healthy-backend gauge at 1 (the partial-placement case).
func TestHealthCheckerModelMetricsIgnoreNonServingBackend(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"object":"list","data":[{"id":"alpha"}]}`)
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{
		"alpha": {u},
		"beta":  {u},
	})
	hc.Metrics = NewMetrics()
	hc.checkAll(context.Background())

	rec := httptest.NewRecorder()
	hc.Metrics.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := rec.Body.String()
	if !strings.Contains(body, `local_ai_proxy_model_healthy_backends{model="alpha"} 1`) {
		t.Errorf("alpha healthy gauge not 1 in:\n%s", body)
	}
	if !strings.Contains(body, `local_ai_proxy_model_healthy_backends{model="beta"} 0`) {
		t.Errorf("beta healthy gauge not 0 in:\n%s", body)
	}
	if !strings.Contains(body, `local_ai_proxy_model_enabled_backends{model="beta"} 1`) {
		t.Errorf("beta enabled gauge not 1 in:\n%s", body)
	}
}

// TestHealthCheckerFetchStateRejectsOversizedBody verifies that a state
// response over the cap fails closed with an explicit over-limit error
// rather than being reported as malformed JSON.
func TestHealthCheckerFetchStateRejectsOversizedBody(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"padded":"`+strings.Repeat("x", 64)+`"}`)
	}))
	defer up.Close()

	u := mustURL(t, up.URL)
	hc := newTestHealthChecker(t, map[string][]*url.URL{"m": {u}})
	var v map[string]json.RawMessage
	err := hc.fetchState(context.Background(), u, "/state/instances", &v, 16)
	if err == nil || !strings.Contains(err.Error(), "exceeds 16 bytes") {
		t.Fatalf("err = %v; want over-limit error", err)
	}
}
