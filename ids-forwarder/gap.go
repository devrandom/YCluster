package main

import (
	"fmt"
	"time"
)

// resolveGap clears the "gap pending" latch after a clean replay, so a
// later rotation loss raises a new alert.
func (f *forwarder) resolveGap() {
	f.gapMu.Lock()
	f.gapPending = false
	f.gapMu.Unlock()
}

// reportGap makes a coverage loss visible. It deliberately uses a
// distinct alertname so the tuning-window silence for IDSAlert does not
// hide it. The latch is only set once the alert is durable: if the spool
// rejects it, the caller sees the error and the process restarts.
func (f *forwarder) reportGap(last time.Time, sources []replaySource, why string) error {
	f.gapMu.Lock()
	already := f.gapPending
	f.gapMu.Unlock()
	if already {
		return nil
	}
	from := "unknown"
	if !last.IsZero() {
		from = last.UTC().Format(time.RFC3339)
	}
	a := newAlert(f.cfg, "forwarder", "warning", "host", "forwarder-gap",
		fmt.Sprintf("IDS replay gap: %s", why))
	a.Labels["alertname"] = "IDSForwarderGap"
	a.Annotations["summary"] = fmt.Sprintf(
		"IDS event replay gap: %s; last processed event %s, %d retained Tetragon file(s)",
		why, from, len(sources))
	a.eventTime = time.Now().UTC()
	a.StartsAt = a.eventTime.Format(time.RFC3339Nano)
	if err := f.enqueue(a); err != nil {
		return err
	}
	f.stats.gaps.Add(1)
	f.gapMu.Lock()
	f.gapPending = true
	f.gapMu.Unlock()
	return nil
}
