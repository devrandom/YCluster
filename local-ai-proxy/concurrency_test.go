package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestUserModelLimiterPerUserPerModel(t *testing.T) {
	l := NewUserModelLimiter(2, 2, nil)

	r1, queued, ok := l.Acquire(context.Background(), "alice", "model-a")
	if !ok || queued {
		t.Fatal("first acquire should be admitted without queueing")
	}
	if _, _, ok := l.Acquire(context.Background(), "alice", "model-a"); !ok {
		t.Fatal("second acquire should be admitted")
	}
	if got := l.InFlight("alice", "model-a"); got != 2 {
		t.Fatalf("InFlight = %d; want 2", got)
	}

	// The limit is per (user, model), not global.
	if _, _, ok := l.Acquire(context.Background(), "alice", "model-b"); !ok {
		t.Fatal("other model should have its own slots")
	}
	if _, _, ok := l.Acquire(context.Background(), "bob", "model-a"); !ok {
		t.Fatal("other user should have its own slots")
	}

	r1()
	if got := l.InFlight("alice", "model-a"); got != 1 {
		t.Fatalf("InFlight after release = %d; want 1", got)
	}
	r3, _, ok := l.Acquire(context.Background(), "alice", "model-a")
	if !ok {
		t.Fatal("acquire after release denied")
	}
	r3()
}

func TestUserModelLimiterDisabled(t *testing.T) {
	l := NewUserModelLimiter(0, 4, nil)
	if l != nil {
		t.Fatal("zero limit should disable the limiter")
	}
	if l.Limit() != 0 || l.MaxQueued() != 0 {
		t.Fatalf("Limit/MaxQueued = %d/%d; want 0/0", l.Limit(), l.MaxQueued())
	}
	for i := 0; i < 100; i++ {
		if _, _, ok := l.Acquire(context.Background(), "alice", "m"); !ok {
			t.Fatal("nil limiter must never deny")
		}
	}
	if got := l.InFlight("alice", "m"); got != 0 {
		t.Fatalf("InFlight = %d; want 0", got)
	}
}

func TestUserModelLimiterEmptyUserUnlimited(t *testing.T) {
	l := NewUserModelLimiter(1, 0, nil)
	for i := 0; i < 5; i++ {
		if _, _, ok := l.Acquire(context.Background(), "", "m"); !ok {
			t.Fatal("requests without an identity must not be limited")
		}
	}
}

func TestUserModelLimiterReleaseIdempotent(t *testing.T) {
	l := NewUserModelLimiter(1, 0, nil)
	release, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("acquire denied")
	}
	release()
	release()
	if got := l.InFlight("alice", "m"); got != 0 {
		t.Fatalf("InFlight = %d; want 0", got)
	}
	if _, _, ok := l.Acquire(context.Background(), "alice", "m"); !ok {
		t.Fatal("double release leaked a slot")
	}
}

func TestUserModelLimiterQueuesUntilSlotFrees(t *testing.T) {
	l := NewUserModelLimiter(1, 2, nil)
	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}

	type result struct {
		release func()
		queued  bool
		ok      bool
	}
	got := make(chan result, 1)
	go func() {
		r, q, ok := l.Acquire(context.Background(), "alice", "m")
		got <- result{r, q, ok}
	}()

	waitFor(t, "waiter to enqueue", func() bool { return l.Queued("alice", "m") == 1 })
	if n := l.InFlight("alice", "m"); n != 1 {
		t.Fatalf("InFlight = %d; want 1 while one waits", n)
	}

	held()
	res := <-got
	if !res.ok || !res.queued {
		t.Fatalf("waiter result ok=%v queued=%v; want admitted via queue", res.ok, res.queued)
	}
	if n := l.Queued("alice", "m"); n != 0 {
		t.Fatalf("Queued = %d after admission; want 0", n)
	}
	if n := l.InFlight("alice", "m"); n != 1 {
		t.Fatalf("InFlight = %d; want 1 (slot handed over)", n)
	}
	res.release()
	if n := l.InFlight("alice", "m"); n != 0 {
		t.Fatalf("InFlight = %d after release; want 0", n)
	}
}

func TestUserModelLimiterQueueFullRejects(t *testing.T) {
	l := NewUserModelLimiter(1, 1, nil)
	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}

	admitted := make(chan struct{})
	go func() {
		release, queued, ok := l.Acquire(context.Background(), "alice", "m")
		if ok && queued {
			close(admitted)
			release()
		}
	}()
	waitFor(t, "queue to fill", func() bool { return l.Queued("alice", "m") == 1 })

	release, queued, ok := l.Acquire(context.Background(), "alice", "m")
	if ok || queued {
		t.Fatalf("third acquire ok=%v queued=%v; want immediate rejection", ok, queued)
	}
	if release != nil {
		t.Fatal("rejected acquire returned a release function")
	}

	held()
	<-admitted
}

func TestUserModelLimiterQueueDisabled(t *testing.T) {
	l := NewUserModelLimiter(1, 0, nil)
	if _, _, ok := l.Acquire(context.Background(), "alice", "m"); !ok {
		t.Fatal("first acquire denied")
	}
	release, queued, ok := l.Acquire(context.Background(), "alice", "m")
	if ok || queued {
		t.Fatalf("zero-depth queue ok=%v queued=%v; want immediate rejection", ok, queued)
	}
	if release != nil {
		t.Fatal("rejected acquire returned a release function")
	}
}

func TestUserModelLimiterCancelWhileQueued(t *testing.T) {
	l := NewUserModelLimiter(1, 1, nil)
	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if release, _, ok := l.Acquire(ctx, "alice", "m"); ok {
			release()
		}
	}()
	waitFor(t, "waiter to enqueue", func() bool { return l.Queued("alice", "m") == 1 })

	cancel()
	<-done
	if n := l.Queued("alice", "m"); n != 0 {
		t.Fatalf("Queued = %d after cancel; want 0", n)
	}
	if n := l.InFlight("alice", "m"); n != 1 {
		t.Fatalf("InFlight = %d after cancellation; want 1 (slot still held)", n)
	}

	held()
	if n := l.InFlight("alice", "m"); n != 0 {
		t.Fatalf("InFlight = %d after release; want 0", n)
	}
	// A cancelled waiter must not consume the slot or leave state behind.
	release, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("acquire after cancelled waiter denied")
	}
	release()
}

// errOnlyContext has a Done channel that never closes but an Err that
// can be flipped. That lets a test hold a waiter in select, then make
// the post-promotion admission check observe cancellation without the
// two select branches racing.
type errOnlyContext struct {
	context.Context
	err atomic.Value
}

func (c *errOnlyContext) Done() <-chan struct{} { return nil }

func (c *errOnlyContext) Err() error {
	if v := c.err.Load(); v != nil {
		return v.(error)
	}
	return nil
}

func TestUserModelLimiterCancelAfterPromotion(t *testing.T) {
	l := NewUserModelLimiter(1, 1, nil)
	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}

	ctx := &errOnlyContext{Context: context.Background()}
	result := make(chan bool, 1)
	go func() {
		_, _, ok := l.Acquire(ctx, "alice", "m")
		result <- ok
	}()
	waitFor(t, "waiter to enqueue", func() bool { return l.Queued("alice", "m") == 1 })

	// Cancel while the waiter is queued, then promote it. Done() never
	// fires, so the waiter necessarily takes the promoted branch and the
	// post-admission check must hand the slot back.
	ctx.err.Store(error(context.Canceled))
	held()
	if ok := <-result; ok {
		t.Fatal("cancelled request was admitted after promotion")
	}
	if n := l.InFlight("alice", "m"); n != 0 {
		t.Fatalf("InFlight = %d after cancelled promotion; want 0", n)
	}
	if n := l.Queued("alice", "m"); n != 0 {
		t.Fatalf("Queued = %d after cancelled promotion; want 0", n)
	}
	release, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("cancelled promoted waiter leaked the slot")
	}
	release()
}

// countingObserver records queue gauge transitions for the limiter.
type countingObserver struct {
	mu  sync.Mutex
	inc map[string]int
	dec map[string]int
}

func newCountingObserver() *countingObserver {
	return &countingObserver{inc: map[string]int{}, dec: map[string]int{}}
}

func (o *countingObserver) QueueInc(model string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.inc[model]++
}

func (o *countingObserver) QueueDec(model string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.dec[model]++
}

func (o *countingObserver) counts(model string) (inc, dec int) {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.inc[model], o.dec[model]
}

func TestUserModelLimiterQueueObserverBalance(t *testing.T) {
	obs := newCountingObserver()
	l := NewUserModelLimiter(1, 1, obs)

	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}
	if inc, dec := obs.counts("m"); inc != 0 || dec != 0 {
		t.Fatalf("fast path touched the queue gauge: inc=%d dec=%d", inc, dec)
	}

	admitted := make(chan struct{})
	go func() {
		release, _, ok := l.Acquire(context.Background(), "alice", "m")
		if ok {
			close(admitted)
			release()
		}
	}()
	waitFor(t, "waiter to enqueue", func() bool { return l.Queued("alice", "m") == 1 })
	if inc, dec := obs.counts("m"); inc != 1 || dec != 0 {
		t.Fatalf("enqueue: inc=%d dec=%d; want 1/0", inc, dec)
	}

	// A full queue rejects without touching the gauge.
	if _, _, ok := l.Acquire(context.Background(), "alice", "m"); ok {
		t.Fatal("full queue admitted a request")
	}
	if inc, dec := obs.counts("m"); inc != 1 || dec != 0 {
		t.Fatalf("rejection: inc=%d dec=%d; want 1/0", inc, dec)
	}

	held()
	<-admitted
	if inc, dec := obs.counts("m"); inc != 1 || dec != 1 {
		t.Fatalf("after admission: inc=%d dec=%d; want 1/1", inc, dec)
	}

	// A cancelled waiter also balances.
	held2, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("re-acquire denied")
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if release, _, ok := l.Acquire(ctx, "alice", "m"); ok {
			release()
		}
	}()
	waitFor(t, "second waiter to enqueue", func() bool { return l.Queued("alice", "m") == 1 })
	cancel()
	<-done
	held2()
	if inc, dec := obs.counts("m"); inc != 2 || dec != 2 {
		t.Fatalf("after cancellation: inc=%d dec=%d; want 2/2", inc, dec)
	}
}

func TestUserModelLimiterFIFO(t *testing.T) {
	l := NewUserModelLimiter(1, 2, nil)
	held, _, ok := l.Acquire(context.Background(), "alice", "m")
	if !ok {
		t.Fatal("first acquire denied")
	}

	order := make(chan int, 2)
	for i := 0; i < 2; i++ {
		i := i
		go func() {
			release, _, ok := l.Acquire(context.Background(), "alice", "m")
			if !ok {
				return
			}
			order <- i
			release()
		}()
		waitFor(t, "waiter to enqueue", func() bool { return l.Queued("alice", "m") == i+1 })
	}

	held()
	if first, second := <-order, <-order; first != 0 || second != 1 {
		t.Fatalf("admission order = %d,%d; want 0,1 (FIFO)", first, second)
	}
}

// TestHandlerUserModelConcurrencyLimit drives the full handler path: two
// in-flight requests fill the cap, the third waits in the queue, the
// fourth is rejected because the queue is full, another user is
// unaffected, and the queued request completes once a slot frees.
func TestHandlerUserModelConcurrencyLimit(t *testing.T) {
	release := make(chan struct{})
	var releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }

	var hits atomic.Int64
	started := make(chan struct{}, 16)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		started <- struct{}{}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer backend.Close()

	h := NewHandler(&fixedRouteRouter{route: &RouteResult{
		Model:      "m",
		Candidates: []*url.URL{mustURL(t, backend.URL)},
	}})
	h.Metrics = NewMetrics()
	h.Concurrency = NewUserModelLimiter(2, 1, h.Metrics)
	srv := httptest.NewServer(h)
	defer srv.Close()
	// Registered after the servers so it runs first on failure: the
	// backend handlers are blocked on release, and their Close would
	// wait for them forever.
	defer unblock()

	client := &http.Client{Timeout: 10 * time.Second}
	post := func(user string) (*http.Response, error) {
		req, err := http.NewRequest(http.MethodPost, srv.URL+"/v1/chat/completions",
			strings.NewReader(`{"model":"m"}`))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if user != "" {
			req.Header.Set("X-User-Id", user)
		}
		return client.Do(req)
	}
	postAndDrain := func(user string, done chan<- error) {
		resp, err := post(user)
		if err != nil {
			done <- err
			return
		}
		defer resp.Body.Close()
		_, _ = io.Copy(io.Discard, resp.Body)
		if resp.StatusCode != http.StatusOK {
			done <- &statusError{user: user, status: resp.StatusCode}
			return
		}
		done <- nil
	}

	errCh := make(chan error, 8)
	var wg sync.WaitGroup
	hold := func(user string) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			postAndDrain(user, errCh)
		}()
	}

	hold("alice")
	hold("alice")
	for i := 0; i < 2; i++ {
		<-started
	}

	// The third request queues; it must not reach the backend yet.
	queuedDone := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		postAndDrain("alice", queuedDone)
	}()
	waitFor(t, "third request to queue", func() bool { return h.Concurrency.Queued("alice", "m") == 1 })
	select {
	case err := <-queuedDone:
		t.Fatalf("queued request returned early: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	if got := hits.Load(); got != 2 {
		t.Fatalf("backend hits = %d; want 2 (queued request must wait)", got)
	}

	// The queue is full, so the fourth request is rejected immediately.
	resp, err := post("alice")
	if err != nil {
		t.Fatalf("fourth request: %v", err)
	}
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("fourth request status = %d; want 429", resp.StatusCode)
	}
	var apiErr openAIError
	if err := json.NewDecoder(resp.Body).Decode(&apiErr); err != nil {
		t.Fatalf("decode error body: %v", err)
	}
	resp.Body.Close()
	if apiErr.Error.Type != "rate_limit_error" || apiErr.Error.Code != "concurrency_limit_exceeded" {
		t.Fatalf("error = %+v; want rate_limit_error/concurrency_limit_exceeded", apiErr.Error)
	}
	if !strings.Contains(metricsBody(h), `local_ai_proxy_user_concurrency_rejected_total{model="m"} 1`) {
		t.Error("full-queue rejection did not increment local_ai_proxy_user_concurrency_rejected_total")
	}

	// A different user has independent slots.
	hold("bob")
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("bob's request did not reach the backend; per-user isolation is broken")
	}

	unblock()
	wg.Wait()
	close(errCh)
	for err := range errCh {
		if err != nil {
			t.Errorf("held request failed: %v", err)
		}
	}
	select {
	case err := <-queuedDone:
		if err != nil {
			t.Errorf("queued request failed: %v", err)
		}
	default:
		t.Fatal("queued request never completed after a slot freed")
	}

	if n := h.Concurrency.InFlight("alice", "m"); n != 0 {
		t.Fatalf("InFlight = %d after all requests; want 0", n)
	}
	if n := h.Concurrency.Queued("alice", "m"); n != 0 {
		t.Fatalf("Queued = %d after all requests; want 0", n)
	}

	body := metricsBody(h)
	if !strings.Contains(body, `local_ai_proxy_user_concurrency_wait_seconds_count{model="m"} 1`) {
		t.Error("queued admission did not observe local_ai_proxy_user_concurrency_wait_seconds")
	}
	if !strings.Contains(body, `local_ai_proxy_user_concurrency_queued{model="m"} 0`) {
		t.Error("queue depth gauge did not return to zero")
	}
}

func metricsBody(h *Handler) string {
	rec := httptest.NewRecorder()
	h.Metrics.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return rec.Body.String()
}

type statusError struct {
	user   string
	status int
}

func (e *statusError) Error() string {
	return e.user + ": unexpected status " + http.StatusText(e.status)
}

// TestHandlerQueuedRequestDisconnect verifies a request whose context is
// cancelled while queued returns without reaching the backend and does
// not consume the slot.
func TestHandlerQueuedRequestDisconnect(t *testing.T) {
	release := make(chan struct{})
	var releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }

	var hits atomic.Int64
	started := make(chan struct{}, 4)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		started <- struct{}{}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer backend.Close()

	h := NewHandler(&fixedRouteRouter{route: &RouteResult{
		Model:      "m",
		Candidates: []*url.URL{mustURL(t, backend.URL)},
	}})
	h.Concurrency = NewUserModelLimiter(1, 1, nil)
	defer unblock()

	request := func(ctx context.Context) *http.Request {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"m"}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-User-Id", "alice")
		return req.WithContext(ctx)
	}

	holderDone := make(chan struct{})
	go func() {
		defer close(holderDone)
		h.ServeHTTP(httptest.NewRecorder(), request(context.Background()))
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("holder request did not reach the backend")
	}

	ctx, cancel := context.WithCancel(context.Background())
	queuedDone := make(chan struct{})
	go func() {
		defer close(queuedDone)
		h.ServeHTTP(httptest.NewRecorder(), request(ctx))
	}()
	waitFor(t, "request to queue", func() bool { return h.Concurrency.Queued("alice", "m") == 1 })

	cancel()
	select {
	case <-queuedDone:
	case <-time.After(5 * time.Second):
		t.Fatal("cancelled queued request did not return")
	}
	if n := h.Concurrency.Queued("alice", "m"); n != 0 {
		t.Fatalf("Queued = %d after cancellation; want 0", n)
	}
	if got := hits.Load(); got != 1 {
		t.Fatalf("cancelled queued request reached the backend: hits = %d; want 1", got)
	}

	unblock()
	select {
	case <-holderDone:
	case <-time.After(5 * time.Second):
		t.Fatal("holder request did not finish")
	}
	if n := h.Concurrency.InFlight("alice", "m"); n != 0 {
		t.Fatalf("InFlight = %d after disconnect; want 0", n)
	}

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, request(context.Background()))
	if rec.Code != http.StatusOK {
		t.Fatalf("post-disconnect status = %d; want 200", rec.Code)
	}
}
