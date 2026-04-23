//go:build system

// System tests exercise the built local-ai-proxy binary against real
// HTTP backends. They spin up httptest servers, launch the proxy as a
// subprocess with a YAML config, and assert behavior over real TCP.
//
// Run with: make test-system (builds first). Excluded from the default
// `go test` run via the `system` build tag so unit tests stay fast.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// pickFreePort grabs an ephemeral port and closes the listener so the
// proxy subprocess can bind it. A tiny race window exists; for tests
// against a single local box it's fine.
func pickFreePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return port
}

// startProxy writes cfgYAML to a temp file, launches the proxy binary,
// waits for /healthz to respond, and registers cleanup. Returns the
// base URL (e.g. "http://127.0.0.1:12345").
func startProxy(t *testing.T, cfgYAML string) string {
	t.Helper()

	// Binary must already be built. Tests run from the package dir.
	bin, err := filepath.Abs("bin/local-ai-proxy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(bin); err != nil {
		t.Fatalf("binary not found at %s — run `make build` first", bin)
	}

	port := pickFreePort(t)
	addr := fmt.Sprintf("127.0.0.1:%d", port)

	cfgPath := filepath.Join(t.TempDir(), "config.yaml")
	full := fmt.Sprintf("listen: %q\n%s\n", addr, cfgYAML)
	if err := os.WriteFile(cfgPath, []byte(full), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, bin, "--config", cfgPath)
	cmd.Stdout = os.Stderr // surface proxy logs to test output
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			_ = cmd.Process.Kill()
		}
	})

	base := "http://" + addr

	// Wait for bind (up to 5s). /healthz is cheap and doesn't need any
	// upstream to be reachable.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			resp.Body.Close()
			return base
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("proxy never became ready at %s", base)
	return ""
}

// fakeBackend wraps an httptest.Server with a mutable handler and a
// hit counter, for tests that flip behavior between requests.
type fakeBackend struct {
	server *httptest.Server
	hits   atomic.Int64
	mu     sync.Mutex
	handle http.HandlerFunc
}

func newFakeBackend(initial http.HandlerFunc) *fakeBackend {
	fb := &fakeBackend{handle: initial}
	fb.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fb.hits.Add(1)
		fb.mu.Lock()
		h := fb.handle
		fb.mu.Unlock()
		h(w, r)
	}))
	return fb
}

func (fb *fakeBackend) setHandler(h http.HandlerFunc) {
	fb.mu.Lock()
	defer fb.mu.Unlock()
	fb.handle = h
}

func (fb *fakeBackend) close() { fb.server.Close() }

// okModels returns 200 OK on /v1/models and on the test path.
func okModels(payload string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, payload)
	}
}

// TestSystemFanoutDistribution — two healthy identical backends serve
// one model; 40 parallel requests should split roughly evenly.
func TestSystemFanoutDistribution(t *testing.T) {
	a := newFakeBackend(okModels(`{"from":"a"}`))
	defer a.close()
	b := newFakeBackend(okModels(`{"from":"b"}`))
	defer b.close()

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
`, a.server.URL, b.server.URL)
	base := startProxy(t, cfg)

	const n = 40
	var wg sync.WaitGroup
	wg.Add(n)
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			resp, err := http.Post(base+"/v1/chat/completions", "application/json",
				strings.NewReader(`{"model":"m","messages":[]}`))
			if err != nil {
				errs <- err
				return
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				errs <- fmt.Errorf("status=%d", resp.StatusCode)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}

	ha, hb := a.hits.Load(), b.hits.Load()
	// Models lookups at startup (Source initialisation) don't exist —
	// Source is YAML so no warmup GET /v1/models hits the backends.
	// All hits should be from the test traffic.
	total := ha + hb
	if total < n {
		t.Errorf("total hits = %d; want at least %d", total, n)
	}
	if ha == 0 || hb == 0 {
		t.Errorf("one backend starved: a=%d b=%d", ha, hb)
	}
	// Load-aware picking should keep splits fairly even under parallel
	// load. Allow generous slack for Go runtime scheduler + timing.
	diff := ha - hb
	if diff < 0 {
		diff = -diff
	}
	if diff > n/2 {
		t.Errorf("split too skewed: a=%d b=%d (diff %d)", ha, hb, diff)
	}
	t.Logf("fan-out split: a=%d b=%d (total=%d, diff=%d)", ha, hb, total, diff)
}

// TestSystemRetryOn5xx — backend A always 503, B always 200. Every
// request that lands on A should transparently succeed via B.
func TestSystemRetryOn5xx(t *testing.T) {
	a := newFakeBackend(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"error":"a down"}`)
	})
	defer a.close()
	b := newFakeBackend(okModels(`{"from":"b"}`))
	defer b.close()

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
`, a.server.URL, b.server.URL)
	base := startProxy(t, cfg)

	const n = 10
	for i := 0; i < n; i++ {
		resp, err := http.Post(base+"/v1/chat/completions", "application/json",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("i=%d: status=%d body=%s", i, resp.StatusCode, body)
		}
		if !strings.Contains(string(body), `"from":"b"`) {
			t.Errorf("i=%d: body=%s; expected B's response", i, body)
		}
	}
	if b.hits.Load() < n {
		t.Errorf("B served %d; want >= %d", b.hits.Load(), n)
	}
	t.Logf("retry-on-5xx: a(503)=%d tried, b(200)=%d served", a.hits.Load(), b.hits.Load())
	if a.hits.Load() == 0 {
		t.Errorf("A was never tried — retry path unexercised")
	}
}

// TestSystemRetryOnTransportError — A is a URL to a closed port
// (unreachable), B is healthy. Proxy should fail over to B every time.
func TestSystemRetryOnTransportError(t *testing.T) {
	// Reserve a port, close the listener — any connect attempt errors.
	deadPort := pickFreePort(t)
	deadURL := fmt.Sprintf("http://127.0.0.1:%d", deadPort)

	b := newFakeBackend(okModels(`{"from":"b"}`))
	defer b.close()

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
`, deadURL, b.server.URL)
	base := startProxy(t, cfg)

	// The initial health pass should have marked deadURL as Down, so
	// most requests route straight to B. But some may still hit the
	// retry path before the health tick. Either way, all should succeed.
	const n = 8
	for i := 0; i < n; i++ {
		resp, err := http.Post(base+"/v1/chat/completions", "application/json",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("i=%d: status=%d body=%s", i, resp.StatusCode, body)
		}
	}
	if b.hits.Load() < n {
		t.Errorf("B served %d; want >= %d", b.hits.Load(), n)
	}
}

// TestSystemAllBackendsFail — every backend returns 429; client should
// see 429 passed through (last-attempt commits), NOT a 502 override.
func TestSystemAllBackendsFail(t *testing.T) {
	make429 := func() http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = io.WriteString(w, `{"error":"rate limited"}`)
		}
	}
	a := newFakeBackend(make429())
	defer a.close()
	b := newFakeBackend(make429())
	defer b.close()

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
`, a.server.URL, b.server.URL)
	base := startProxy(t, cfg)

	resp, err := http.Post(base+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"m"}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Errorf("status = %d; want 429 (upstream passed through)", resp.StatusCode)
	}
	if !strings.Contains(string(body), "rate limited") {
		t.Errorf("body = %s; want upstream error forwarded", body)
	}
	// Both backends were tried (2 attempts per request).
	ha, hb := a.hits.Load(), b.hits.Load()
	total := ha + hb
	if total < 2 {
		t.Errorf("total hits = %d; want 2 (both backends tried)", total)
	}
	if ha == 0 || hb == 0 {
		t.Errorf("only one backend tried: a=%d b=%d; both should be attempted", ha, hb)
	}
	t.Logf("all-fail: a=%d hits, b=%d hits (total %d for 1 request + 2 startup probes)", ha, hb, total)
}

// TestSystemMetricsEndpoint — after driving some traffic through a
// fan-out pool, /metrics should expose per-backend counters, gauges,
// and retries in Prometheus text format.
func TestSystemMetricsEndpoint(t *testing.T) {
	good := newFakeBackend(okModels(`{"ok":true}`))
	defer good.close()
	// flaky: passes health probe (GET /v1/models) so it stays in the
	// routable set, but 503s on real traffic — forces the retry path.
	flaky := newFakeBackend(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/models" {
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{}`)
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	defer flaky.close()

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
health_check_interval: 1h
`, good.server.URL, flaky.server.URL)
	base := startProxy(t, cfg)

	// Drive a handful of requests to populate metrics. Every request
	// that lands on `flaky` retries to `good`, so we should see 5xx
	// retries in the counter. (After the first retry, the handler's
	// Probe call marks flaky Down, so subsequent requests go straight
	// to good — one retry is enough to populate the metric.)
	const n = 12
	for i := 0; i < n; i++ {
		resp, err := http.Post(base+"/v1/chat/completions", "application/json",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	// One request with an unknown model — populates route_errors.
	resp, _ := http.Post(base+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"ghost"}`))
	if resp != nil {
		resp.Body.Close()
	}

	resp, err := http.Get(base + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("/metrics status = %d", resp.StatusCode)
	}
	text := string(body)

	// Check every metric family we expose is present with at least
	// one sample. We don't assert exact values — schedule / timing
	// affect which backend each request lands on first.
	for _, want := range []string{
		"local_ai_proxy_requests_total",
		"local_ai_proxy_request_duration_seconds_bucket",
		"local_ai_proxy_retries_total",
		"local_ai_proxy_inflight",
		"local_ai_proxy_backend_healthy",
		"local_ai_proxy_route_errors_total",
		`local_ai_proxy_route_errors_total{reason="unknown_model"} 1`,
	} {
		if !strings.Contains(text, want) {
			t.Errorf("/metrics missing %q", want)
		}
	}

	// Good backend is healthy (1). Flaky one: startup probe saw a
	// 200 on /v1/models so it started as 1, but the Probe fired on
	// retry will have re-checked and still found 200 — so it stays 1.
	// (Demonstrates that Probe is a health-level concern, not a
	// request-level penalty.)
	goodGauge := fmt.Sprintf(`local_ai_proxy_backend_healthy{backend="%s"} 1`, good.server.URL)
	if !strings.Contains(text, goodGauge) {
		t.Errorf("/metrics missing %q", goodGauge)
	}

	// Retries should have fired on 503s; count not asserted precisely
	// but should exist.
	if !strings.Contains(text, `reason="http_5xx"`) {
		t.Errorf("/metrics missing retry label reason=http_5xx")
	}

	// Standard Go/process collectors should also be present.
	for _, want := range []string{"go_goroutines", "process_resident_memory_bytes"} {
		if !strings.Contains(text, want) {
			t.Errorf("/metrics missing standard metric %q", want)
		}
	}
}

// TestSystemHealthzReflectsState — after one initial health pass,
// /healthz should show the live backend as healthy and the dead one
// as down.
func TestSystemHealthzReflectsState(t *testing.T) {
	b := newFakeBackend(okModels(`{}`))
	defer b.close()
	dead := fmt.Sprintf("http://127.0.0.1:%d", pickFreePort(t))

	cfg := fmt.Sprintf(`backends:
  - model: m
    api_base: %q
  - model: m
    api_base: %q
health_check_interval: 250ms
`, b.server.URL, dead)
	base := startProxy(t, cfg)

	// Wait for at least one health pass to have run (Start runs
	// synchronously; still give the subprocess a moment).
	var got struct {
		Backends []struct {
			URL   string `json:"url"`
			State string `json:"state"`
		} `json:"backends"`
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(base + "/healthz")
		if err != nil {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		_ = json.NewDecoder(resp.Body).Decode(&got)
		resp.Body.Close()
		if len(got.Backends) == 2 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	if len(got.Backends) != 2 {
		t.Fatalf("got %d backends in /healthz; want 2", len(got.Backends))
	}
	states := map[string]string{}
	for _, e := range got.Backends {
		states[e.URL] = e.State
	}
	if states[b.server.URL] != "healthy" {
		t.Errorf("healthy backend %s: state=%q; want healthy", b.server.URL, states[b.server.URL])
	}
	if states[dead] != "down" {
		t.Errorf("dead backend %s: state=%q; want down", dead, states[dead])
	}
}
