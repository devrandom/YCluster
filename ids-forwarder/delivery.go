package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

func (f *forwarder) postLoop() {
	defer f.wg.Done()
	for !f.stopping() {
		var keepGoing bool
		if f.spool != nil {
			keepGoing = f.postSpoolItem()
		} else {
			keepGoing = f.postQueueItem()
		}
		if !keepGoing {
			return
		}
	}
}

func (f *forwarder) postSpoolItem() bool {
	if f.stopping() {
		return false
	}
	item, ok, err := f.spool.next()
	if err != nil {
		switch {
		case errors.Is(err, errSpoolCorrupt):
			f.stats.dropped.Add(1)
			f.cfg.logf("spool entry skipped: %v", err)
			return true
		case errors.Is(err, errSpoolEntryTooLarge):
			f.stats.oversized.Add(1)
			f.stats.dropped.Add(1)
			f.cfg.logf("spool entry skipped: %v", err)
			return true
		default:
			// Losing the ability to read the spool means alerts may be
			// lost from the durable queue; let systemd restart us.
			f.fail(fmt.Errorf("read spool: %w", err))
			return false
		}
	}
	if !ok {
		return f.wait(f.cfg.pollInterval)
	}
	if f.stopping() {
		// Leave the item unacked so the next start replays it.
		return false
	}
	if f.dropIfOld(item.alert) {
		return f.ackSpool(item.token)
	}
	if err := f.post([]alert{item.alert}); err != nil {
		var perm *permanentPostError
		if errors.As(err, &perm) {
			// Re-posting the same record cannot succeed; consume it so
			// the rest of the spool is not blocked behind it.
			f.stats.rejected.Add(1)
			f.stats.dropped.Add(1)
			f.cfg.logf("alertmanager rejected alert %s: %v; dropping", item.alert.Labels["rule_id"], err)
			return f.ackSpool(item.token)
		}
		f.cfg.logf("post to alertmanager: %v", err)
		return f.wait(f.retryInterval())
	}
	f.stats.forwarded.Add(1)
	return f.ackSpool(item.token)
}

func (f *forwarder) postQueueItem() bool {
	a, ok := f.queue.pop()
	if !ok {
		return f.wait(f.cfg.pollInterval)
	}
	if f.dropIfOld(a) {
		return true
	}
	if err := f.post([]alert{a}); err != nil {
		f.cfg.logf("post to alertmanager: %v", err)
		if !f.queue.pushFront(a) {
			f.stats.dropped.Add(1)
		}
		return f.wait(f.retryInterval())
	}
	f.stats.forwarded.Add(1)
	return true
}

func (f *forwarder) dropIfOld(a alert) bool {
	if !a.tooOld(time.Now(), f.cfg.maxEventAge) {
		return false
	}
	f.stats.expired.Add(1)
	f.cfg.logf("dropping event older than %s: %s", f.cfg.maxEventAge, a.Labels["rule_id"])
	return true
}

func (f *forwarder) ackSpool(token int64) bool {
	if err := f.spool.ackTo(token); err != nil {
		f.fail(fmt.Errorf("ack spool: %w", err))
		return false
	}
	return true
}

func (f *forwarder) retryInterval() time.Duration {
	return f.cfg.pollInterval * 4
}

// wait sleeps for d unless shutdown is requested.
func (f *forwarder) wait(d time.Duration) bool {
	select {
	case <-f.stop:
		return false
	case <-time.After(d):
		return true
	}
}

func (f *forwarder) post(alerts []alert) error {
	if len(alerts) == 0 {
		return nil
	}
	// EndsAt is assigned when the alert is actually delivered. A queued
	// alert may wait during an Alertmanager outage; its active window
	// should start then, not when the source event was observed.
	now := time.Now().UTC()
	for i := range alerts {
		alerts[i].EndsAt = now.Add(f.cfg.alertTTL).Format(time.RFC3339Nano)
	}
	body, err := json.Marshal(alerts)
	if err != nil {
		return err
	}
	// alertmanager-url may be a comma-separated list (the cluster runs
	// Alertmanager on every core node). Try each in order; a single
	// reachable instance is enough for the alert to be accepted.
	var lastErr error
	for _, base := range strings.Split(f.cfg.alertmanagerURL, ",") {
		base = strings.TrimSpace(base)
		if base == "" {
			continue
		}
		if err := f.postOne(base, body); err != nil {
			var perm *permanentPostError
			if errors.As(err, &perm) {
				f.stats.failures.Add(1)
				return err
			}
			lastErr = err
			continue
		}
		return nil
	}
	f.stats.failures.Add(1)
	if lastErr == nil {
		lastErr = errors.New("no alertmanager URL configured")
	}
	return lastErr
}

func (f *forwarder) postOne(base string, body []byte) error {
	url := strings.TrimRight(base, "/") + "/api/v2/alerts"
	ctx, cancel := context.WithTimeout(context.Background(), f.cfg.postTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := f.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		if resp.StatusCode >= 400 && resp.StatusCode < 500 &&
			resp.StatusCode != http.StatusRequestTimeout &&
			resp.StatusCode != http.StatusTooManyRequests {
			return &permanentPostError{status: resp.Status}
		}
		return fmt.Errorf("alertmanager returned %s", resp.Status)
	}
	return nil
}

// permanentPostError means Alertmanager rejected the payload itself, so
// retrying the same record cannot help. Transient statuses (408, 429,
// 5xx) and transport errors are not permanent.
type permanentPostError struct {
	status string
}

func (e *permanentPostError) Error() string {
	return fmt.Sprintf("alertmanager rejected the alert permanently: %s", e.status)
}
