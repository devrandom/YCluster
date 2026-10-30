package main

import (
	"sync"
	"time"
)

// rateLimiter caps how many alerts of each severity are delivered per
// window. Events that exceed the cap are still recorded: the limiter is a
// storm brake for the paging path, not a filter.
type rateLimiter struct {
	mu     sync.Mutex
	limit  int
	window time.Duration
	times  map[string][]time.Time
}

func newRateLimiter(limit int, window time.Duration) *rateLimiter {
	return &rateLimiter{
		limit:  limit,
		window: window,
		times:  make(map[string][]time.Time),
	}
}

// allow reserves a delivery slot for severity, reporting whether one was
// available. Each severity has an independent budget, so a warning storm
// cannot consume the capacity reserved for critical alerts.
func (r *rateLimiter) allow(severity string) bool {
	if r == nil || r.limit <= 0 {
		return true
	}
	now := time.Now()
	cutoff := now.Add(-r.window)
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.times == nil {
		r.times = make(map[string][]time.Time)
	}
	kept := r.times[severity][:0]
	for _, t := range r.times[severity] {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	r.times[severity] = kept
	if len(kept) >= r.limit {
		return false
	}
	r.times[severity] = append(kept, now)
	return true
}
