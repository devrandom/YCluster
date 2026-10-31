package main

import (
	"context"
	"sync"
)

// DefaultMaxConcurrentPerUserModel caps in-flight requests per
// (X-User-Id, model) pair when the config field is unset. Four keeps a
// single client from occupying every batch slot on a shared backend
// while still letting agent-style workloads pipeline a few requests.
const DefaultMaxConcurrentPerUserModel = 4

// DefaultMaxQueuedPerUserModel bounds how many requests may wait for a
// slot per (X-User-Id, model) pair when the config field is unset.
// Queueing absorbs an agent's burst instead of failing it; the depth
// caps the waiting connections and queue state. The router buffers
// request bodies to find the model before admission, so this is not a
// memory bound by itself.
const DefaultMaxQueuedPerUserModel = 4

// userModelKey identifies one caller's usage of one model.
type userModelKey struct {
	user  string
	model string
}

// QueueObserver is notified when a request enters or leaves a wait
// queue. model is the model the request was routed to. The limiter does
// not depend on the metrics implementation; a nil observer is fine.
type QueueObserver interface {
	QueueInc(model string)
	QueueDec(model string)
}

// UserModelLimiter caps in-flight requests per (user, model) pair and
// queues a bounded number of excess requests until a slot frees.
//
// Its purpose is fairness, not admission control at the edge: a
// backend's batch slots are shared, so N parallel streams from one user
// make each of those streams slower and crowd everyone else out.
// Capping each caller keeps a single runaway workload from consuming
// the whole node, and the queue is per caller, so nobody else waits
// behind it.
//
// Limiting is disabled by a nil *UserModelLimiter (NewUserModelLimiter
// returns nil for a non-positive limit), and all methods are nil-safe.
// Requests without a trusted X-User-Id (health probes, unauthenticated
// local calls) are never limited.
type UserModelLimiter struct {
	limit     int
	maxQueued int
	observer  QueueObserver

	mu    sync.Mutex
	gates map[userModelKey]*userModelGate
}

// NewUserModelLimiter returns a limiter with the given per-(user, model)
// cap and wait-queue depth, or nil (limiting disabled) when limit <= 0.
// maxQueued <= 0 disables queueing: over-cap requests are rejected.
func NewUserModelLimiter(limit, maxQueued int, observer QueueObserver) *UserModelLimiter {
	if limit <= 0 {
		return nil
	}
	return &UserModelLimiter{
		limit:     limit,
		maxQueued: maxQueued,
		observer:  observer,
		gates:     make(map[userModelKey]*userModelGate),
	}
}

// Limit returns the configured in-flight cap, or 0 when limiting is
// disabled.
func (l *UserModelLimiter) Limit() int {
	if l == nil {
		return 0
	}
	return l.limit
}

// MaxQueued returns the configured wait-queue depth, or 0 when limiting
// is disabled or queueing is turned off.
func (l *UserModelLimiter) MaxQueued() int {
	if l == nil {
		return 0
	}
	return l.maxQueued
}

// Acquire reserves a slot for user on model, waiting in the caller's
// FIFO queue when the cap is reached. queued reports whether this call
// had to wait. ok is false when the queue is full (immediate rejection)
// or when ctx ended while waiting (the client is gone).
//
// A nil limiter or empty user admits immediately without queueing.
// Release must be called exactly once when the request finishes; it is
// safe to call more than once.
func (l *UserModelLimiter) Acquire(ctx context.Context, user, model string) (release func(), queued, ok bool) {
	if l == nil || user == "" {
		return func() {}, false, true
	}
	k := userModelKey{user: user, model: model}

	l.mu.Lock()
	g := l.gates[k]
	if g == nil {
		g = &userModelGate{}
		l.gates[k] = g
	}
	if g.running < l.limit {
		g.running++
		l.mu.Unlock()
		return l.releaser(k, g), false, true
	}
	if len(g.waiters) >= l.maxQueued || ctx.Err() != nil {
		l.dropGateIfIdle(k, g)
		l.mu.Unlock()
		return nil, false, false
	}
	ch := make(chan struct{})
	g.waiters = append(g.waiters, ch)
	l.mu.Unlock()
	l.observeQueue(model, 1)

	select {
	case <-ch:
		if ctx.Err() != nil {
			// The client went away as the slot was handed over; pass
			// it to the next waiter instead of running work nobody
			// is waiting for.
			l.releaseSlot(k, g)
			return nil, true, false
		}
		return l.releaser(k, g), true, true
	case <-ctx.Done():
		l.mu.Lock()
		if removeWaiter(g, ch) {
			l.dropGateIfIdle(k, g)
			l.mu.Unlock()
			l.observeQueue(model, -1)
			return nil, true, false
		}
		l.mu.Unlock()
		// Admitted concurrently with the cancellation: hand the slot
		// to the next waiter. The promotion already decremented the
		// queue gauge.
		l.releaseSlot(k, g)
		return nil, true, false
	}
}
