package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeHealthy is a Healthy double with a fixed per-URL allowlist.
type fakeHealthy struct{ healthy map[string]bool }

func (f *fakeHealthy) IsHealthy(u string) bool { return f.healthy[u] }

// TestModelRouterPicksLeastLoaded verifies that with multiple healthy
// backends, the router picks the one with the lowest in-flight count.
func TestModelRouterPicksLeastLoaded(t *testing.T) {
	a := mustURL(t, "http://a.example:8000")
	b := mustURL(t, "http://b.example:8000")
	src := &fakeSource{m: map[string][]*url.URL{"m": {a, b}}}
	lc := NewLoadCounter()
	// Pretend backend A already has 3 in-flight; B has 0.
	for i := 0; i < 3; i++ {
		lc.Inc(a.String())
	}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{a.String(): true, b.String(): true}}
	r.Load = lc

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"m"}`))
	res, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	got := PickBackend(res.Candidates, lc)
	if got.String() != b.String() {
		t.Errorf("picked %s; want %s (least-loaded)", got, b)
	}
}

// TestModelRouterSkipsUnhealthy verifies that unhealthy backends are
// excluded from routing candidates.
func TestModelRouterSkipsUnhealthy(t *testing.T) {
	good := mustURL(t, "http://good:8000")
	bad := mustURL(t, "http://bad:8000")
	src := &fakeSource{m: map[string][]*url.URL{"m": {bad, good}}}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{good.String(): true}}

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"m"}`))
	res, err := r.Route(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Candidates) != 1 || res.Candidates[0].String() != good.String() {
		t.Errorf("candidates = %v; want [%s]", res.Candidates, good)
	}
}

// TestModelRouterAllUnhealthyReturnsSentinel verifies that when every
// backend is unhealthy, Route returns ErrNoHealthyBackend so the
// handler can surface it as 503 rather than 400.
func TestModelRouterAllUnhealthyReturnsSentinel(t *testing.T) {
	a := mustURL(t, "http://a:8000")
	src := &fakeSource{m: map[string][]*url.URL{"m": {a}}}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{}} // nothing healthy

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"m"}`))
	_, err := r.Route(req)
	if !errors.Is(err, ErrNoHealthyBackend) {
		t.Errorf("err = %v; want ErrNoHealthyBackend", err)
	}
}

// TestHandlerAllBackendsDownReturns503 exercises the full path: every
// backend for a model is unhealthy → handler returns 503, not 400.
func TestHandlerAllBackendsDownReturns503(t *testing.T) {
	dead := mustURL(t, "http://127.0.0.1:1") // no listener

	src := &fakeSource{m: map[string][]*url.URL{"m": {dead}}}
	hc := NewHealthChecker(src, time.Hour, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	hc.checkAll(context.Background()) // will mark dead as Down

	r := NewModelRouter(src)
	r.Healthy = hc

	h := NewHandler(r)
	h.Health = hc
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"m"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("status = %d; want 503", resp.StatusCode)
	}
}

// TestFanoutDistributesParallelLoad verifies that concurrent requests
// to a single model spread across its healthy backends rather than
// piling on one. Each upstream holds the request open so in-flight
// counts accumulate before any completes — forcing the router to pick
// the less-loaded backend on subsequent requests.
func TestFanoutDistributesParallelLoad(t *testing.T) {
	var hitA, hitB atomic.Int64
	release := make(chan struct{})

	mkBackend := func(counter *atomic.Int64) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			counter.Add(1)
			<-release // hold request open until test releases
			w.WriteHeader(http.StatusOK)
		}))
	}
	a := mkBackend(&hitA)
	defer a.Close()
	b := mkBackend(&hitB)
	defer b.Close()

	aURL := mustURL(t, a.URL)
	bURL := mustURL(t, b.URL)
	src := &fakeSource{m: map[string][]*url.URL{"m": {aURL, bURL}}}
	lc := NewLoadCounter()

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{aURL.String(): true, bURL.String(): true}}
	r.Load = lc

	h := NewHandler(r)
	h.Load = lc
	proxy := httptest.NewServer(h)
	defer proxy.Close()

	const n = 20
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
				strings.NewReader(`{"model":"m"}`))
			if err != nil {
				t.Error(err)
				return
			}
			resp.Body.Close()
		}()
	}

	// Let all requests reach their backends.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if hitA.Load()+hitB.Load() >= n {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	close(release)
	wg.Wait()

	total := hitA.Load() + hitB.Load()
	if total != n {
		t.Fatalf("total requests = %d; want %d", total, n)
	}
	// With load-aware picking, we expect a near-even split. Allow some
	// slack for timing jitter, but neither side should be starved.
	if hitA.Load() == 0 || hitB.Load() == 0 {
		t.Errorf("one backend starved: A=%d B=%d", hitA.Load(), hitB.Load())
	}
	if diff := hitA.Load() - hitB.Load(); diff > 4 || diff < -4 {
		t.Errorf("split too uneven: A=%d B=%d", hitA.Load(), hitB.Load())
	}

	// Counters should return to zero after all requests complete.
	if c := lc.Count(aURL.String()); c != 0 {
		t.Errorf("A in-flight = %d after completion; want 0", c)
	}
	if c := lc.Count(bURL.String()); c != 0 {
		t.Errorf("B in-flight = %d after completion; want 0", c)
	}
}

// TestRetryOn5xxFailsOverToHealthyPeer: if backend A returns 503, the
// handler should transparently retry against backend B and the client
// sees a 200.
func TestRetryOn5xxFailsOverToHealthyPeer(t *testing.T) {
	var hitA, hitB atomic.Int64
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hitA.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer a.Close()
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hitB.Add(1)
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "from-b")
	}))
	defer b.Close()

	aURL := mustURL(t, a.URL)
	bURL := mustURL(t, b.URL)
	src := &fakeSource{m: map[string][]*url.URL{"m": {aURL, bURL}}}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{aURL.String(): true, bURL.String(): true}}

	h := NewHandler(r)
	proxy := httptest.NewServer(h)
	defer proxy.Close()

	// Run several requests so we exercise the case where A is picked
	// first at least once (load-aware + tie-break makes A or B equally
	// likely on the first call).
	for i := 0; i < 5; i++ {
		resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("attempt %d: status = %d; want 200 via failover", i, resp.StatusCode)
		}
		if string(body) != "from-b" {
			t.Errorf("attempt %d: body = %q; want from-b", i, body)
		}
	}
	if hitB.Load() != 5 {
		t.Errorf("B served %d; want 5", hitB.Load())
	}
	// A should have been picked at least once and failed over.
	if hitA.Load() == 0 {
		t.Log("A never picked — acceptable but skipped the retry path")
	}
}

// TestRetryOn4xxFailsOverToHealthyPeer: a 4xx from one backend (e.g.
// config drift: model missing on that replica) should fall over to a
// peer, not be returned to the client.
func TestRetryOn4xxFailsOverToHealthyPeer(t *testing.T) {
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer a.Close()
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "from-b")
	}))
	defer b.Close()

	aURL := mustURL(t, a.URL)
	bURL := mustURL(t, b.URL)
	src := &fakeSource{m: map[string][]*url.URL{"m": {aURL, bURL}}}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{aURL.String(): true, bURL.String(): true}}

	h := NewHandler(r)
	proxy := httptest.NewServer(h)
	defer proxy.Close()

	for i := 0; i < 5; i++ {
		resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("attempt %d: status = %d; want 200 via failover from 404", i, resp.StatusCode)
		}
		if string(body) != "from-b" {
			t.Errorf("attempt %d: body = %q", i, body)
		}
	}
}

// TestRetryLastAttemptCommitsError: if every backend fails, the last
// one's response is committed to the client (not converted to 502),
// so the client gets an informative status code.
func TestRetryLastAttemptCommitsError(t *testing.T) {
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = io.WriteString(w, "a-was-here")
	}))
	defer a.Close()
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = io.WriteString(w, "b-was-here")
	}))
	defer b.Close()

	aURL := mustURL(t, a.URL)
	bURL := mustURL(t, b.URL)
	src := &fakeSource{m: map[string][]*url.URL{"m": {aURL, bURL}}}

	r := NewModelRouter(src)
	r.Healthy = &fakeHealthy{healthy: map[string]bool{aURL.String(): true, bURL.String(): true}}

	h := NewHandler(r)
	proxy := httptest.NewServer(h)
	defer proxy.Close()

	resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"m"}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	// One of the two backend responses should reach the client (whichever
	// was tried last). Not a 502 override.
	if resp.StatusCode == http.StatusBadGateway {
		t.Errorf("status = 502; want the last backend's actual status passed through")
	}
	if resp.StatusCode != http.StatusTooManyRequests && resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status = %d; want 429 or 400", resp.StatusCode)
	}
	if !strings.Contains(string(body), "was-here") {
		t.Errorf("body = %q; want the upstream error body forwarded", body)
	}
}

// TestLoadCounterIncDecRoundTrip sanity-checks the atomic counter
// semantics, since it's shared between handler and router.
func TestLoadCounterIncDecRoundTrip(t *testing.T) {
	lc := NewLoadCounter()
	if c := lc.Count("x"); c != 0 {
		t.Errorf("fresh count = %d; want 0", c)
	}
	lc.Inc("x")
	lc.Inc("x")
	if c := lc.Count("x"); c != 2 {
		t.Errorf("after 2 Inc: count = %d; want 2", c)
	}
	lc.Dec("x")
	if c := lc.Count("x"); c != 1 {
		t.Errorf("after Dec: count = %d; want 1", c)
	}
}
