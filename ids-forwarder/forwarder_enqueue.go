package main

import (
	"errors"
	"time"
)

// ruleRepeatWindows coalesces repeated detections of the same shape. Rules
// absent from the map use the shared dedup window only.
var ruleRepeatWindows = map[string]time.Duration{
	"container-exec": time.Hour,
	"container-api":  time.Hour,
}

// enqueue makes one alert durable. It returns an error only when the
// alert could not be stored; callers must then keep their source cursor
// where it is so the event is retried.
func (f *forwarder) enqueue(a alert) error {
	// API usage is an audit trail before it is an alert: record every call,
	// even when delivery is deduplicated.
	auditOnly := a.Labels["rule_id"] == "container-api"
	if auditOnly {
		f.recordDetection(a)
	}
	key := a.dedupKey()
	if f.dedup.seen(key) {
		f.stats.deduped.Add(1)
		return nil
	}
	// A recurring action describes the same thing happening again, not a new
	// condition. Alert on the first occurrence in the window and keep
	// recording the rest, so a scheduled exec does not page on every run.
	if window, ok := ruleRepeatWindows[a.Labels["rule_id"]]; ok && f.dedup.seenWithin(key, window) {
		f.stats.coalesced.Add(1)
		if !auditOnly {
			f.recordDetection(a)
		}
		return nil
	}
	// Optional per-host circuit breaker: deliver at most rate-limit alerts
	// per severity per window, but keep recording everything so the history
	// stays complete. The cluster-wide notification budget lives in
	// ntfy-webhook, which is the only shared enforcement point; this local
	// cap is disabled in the Ansible deployment. The audit rule already
	// coalesces to one alert per hour and must not crowd out detections that
	// need a human.
	if !auditOnly && !f.rate.allow(a.Labels["severity"]) {
		f.stats.rateLimited.Add(1)
		if !auditOnly {
			f.recordDetection(a)
		}
		return nil
	}
	if f.spool != nil {
		if err := f.spool.append(a); err != nil {
			if errors.Is(err, errSpoolEntryTooLarge) {
				// A single pathological event must not wedge replay.
				f.stats.oversized.Add(1)
				f.stats.dropped.Add(1)
				f.cfg.logf("drop oversized alert %s: %v", a.Labels["rule_id"], err)
				return nil
			}
			f.stats.dropped.Add(1)
			return err
		}
		// Mark only after the append is durable: a failed append must stay
		// retryable instead of being suppressed as a duplicate.
		f.dedup.mark(key)
		if !auditOnly {
			f.recordDetection(a)
		}
		return nil
	}
	if !f.queue.push(a) {
		f.stats.dropped.Add(1)
		f.cfg.logf("queue full, dropping alert")
		return nil
	}
	f.dedup.mark(key)
	if !auditOnly {
		f.recordDetection(a)
	}
	return nil
}

// recordDetection appends the detection to the local record file. This is
// the durable history of what the sensor classified as a detection, kept
// separately from Alertmanager, which is only the paging path. A write
// failure is counted and logged rather than returned: a full disk must not
// stall the cursor or stop coverage alerts from being delivered.
func (f *forwarder) recordDetection(a alert) {
	if f.record == nil {
		return
	}
	if err := f.record.append(newDetectionRecord(a)); err != nil {
		f.stats.recordErrors.Add(1)
		f.cfg.logf("record detection %s: %v", a.Labels["rule_id"], err)
		return
	}
	f.stats.recorded.Add(1)
}
