package main

import (
	"context"
	"net/url"
)

// Probe runs an out-of-band health check on u in a goroutine and
// returns immediately. Used by the request handler to nudge the
// checker after a retry: the checker is the single writer to the
// state map, and an immediate re-probe means a transient backend
// glitch doesn't strand traffic until the next tick.
//
// Disabled backends are skipped. If the checker is shutting down
// the probe is a no-op.
func (hc *HealthChecker) Probe(u *url.URL) {
	if hc.shouldProbe(u) {
		go hc.checkOne(context.Background(), u)
	}
}

// shouldProbe guards an out-of-band probe: nil receivers/targets, disabled
// backends, and a checker that is shutting down are all skipped.
func (hc *HealthChecker) shouldProbe(u *url.URL) bool {
	if hc == nil || u == nil {
		return false
	}
	if hc.Disabled != nil && hc.Disabled.IsDisabled(u.String()) {
		return false
	}
	if hc.done == nil {
		return true
	}
	select {
	case <-hc.done:
		return false
	default:
		return true
	}
}
