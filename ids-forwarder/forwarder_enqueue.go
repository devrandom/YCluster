package main

import (
	"errors"
	"time"
)

// ruleRepeatWindows coalesces repeated detections of the same shape. Rules
// absent from the map use the shared dedup window only.
var ruleRepeatWindows = map[string]time.Duration{
	"process_exec":   time.Hour,
	"tcp-connect":    time.Hour,
	"container-exec": time.Hour,
	"container-api":  time.Hour,
}

// enqueue makes one alert durable. It returns an error only when the
// alert could not be stored; callers must then keep their source cursor
// where it is so the event is retried.
func (f *forwarder) enqueue(a alert) error {
	// The detection history is an audit trail before it is an alert:
	// record every classified detection, including the ones whose delivery
	// is deduplicated or coalesced.
	class, evidence := classifyAlert(a)
	delivery := deliveryFor(a, class, evidence)
	a.Labels["class"] = class
	// No delivery label travels with the alert: whether it pages is decided
	// here, and a finding that does not page is never posted at all, so the
	// route table has nothing left to decide.
	// The summary is the notification title and has to stay short; the
	// description carries the context a reader needs to triage without
	// opening detections.jsonl on the host.
	if a.Annotations == nil {
		a.Annotations = map[string]string{}
	}
	a.Annotations["description"] = alertDescription(a, class, delivery)
	f.recordDetection(a, class, delivery)
	if delivery == deliveryEvidence {
		// Post-root host activity: recorded, never delivered. Dropping it
		// before the dedup and rate paths keeps it from spending a key or a
		// slot that a later coverage event needs.
		f.stats.countEvidence(class)
		return nil
	}
	if f.bootSuppress(a) {
		f.stats.bootSuppressed.Add(1)
		return nil
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
		return nil
	}
	if delivery == deliverySilent {
		// Alertmanager only ever sees what may notify someone: a detection
		// nobody can act on is the durable record, not an alert. Counting
		// here measures what enabling the class would actually page, dedup
		// and coalescing included, and the key is still marked so a repeat
		// inside the window is deduplicated rather than counted twice.
		f.stats.countPageCandidate(class)
		f.dedup.mark(key)
		return nil
	}
	// Optional per-host circuit breaker: page at most rate-limit alerts per
	// severity per window, but keep recording everything so the history stays
	// complete. Only a paging class reaches this point, so neither the audit
	// trail nor a suppressed class can spend a slot. The cluster-wide
	// notification budget lives in ntfy-webhook, which is the only shared
	// enforcement point; this local cap is disabled in the Ansible deployment.
	if !f.rate.allow(a.Labels["severity"]) {
		f.stats.rateLimited.Add(1)
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
		return nil
	}
	if !f.queue.push(a) {
		f.stats.dropped.Add(1)
		f.cfg.logf("queue full, dropping alert")
		return nil
	}
	f.dedup.mark(key)
	return nil
}

// bootSuppress keeps the routine activity a host emits while it is coming up
// out of Alertmanager. The event is already in detections.jsonl. The set is
// deliberately narrow: private lateral connects, file writes, Sigma rules,
// forwarder gaps and interactive sessions are never hidden by boot time.
func (f *forwarder) bootSuppress(a alert) bool {
	if f.cfg.bootGrace <= 0 || f.bootTime.IsZero() || a.eventTime.IsZero() {
		return false
	}
	if a.eventTime.Before(f.bootTime) || a.eventTime.After(f.bootTime.Add(f.cfg.bootGrace)) {
		return false
	}
	if a.Labels["origin"] == "interactive" {
		return false
	}
	switch a.Labels["rule_id"] {
	case "container-api", "container-exec", "kernel-modules", "privilege-raises":
		return true
	case "process_exec":
		switch a.Labels["origin"] {
		case "unit", "unknown":
			return true
		}
	}
	return false
}

// recordDetection appends the detection to the local record file. This is
// the durable history of what the sensor classified as a detection, kept
// separately from Alertmanager, which is only the paging path. A write
// failure is counted and logged rather than returned: a full disk must not
// stall the cursor or stop coverage alerts from being delivered.
func (f *forwarder) recordDetection(a alert, class, delivery string) {
	if f.record == nil {
		return
	}
	if err := f.record.append(newDetectionRecord(a, class, delivery)); err != nil {
		f.stats.recordErrors.Add(1)
		f.cfg.logf("record detection %s: %v", a.Labels["rule_id"], err)
		return
	}
	f.stats.recorded.Add(1)
}
