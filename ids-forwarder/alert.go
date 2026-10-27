package main

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// alert is an Alertmanager v2 alert payload. The unexported fields carry
// the source event's position through the in-memory queue; persisted
// alerts recover the event time from StartsAt.
type alert struct {
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
	StartsAt    string            `json:"startsAt"`
	EndsAt      string            `json:"endsAt"`

	eventTime time.Time
	eventHash string
}

// eventTimestamp returns the event's original time. Spooled alerts lose
// the unexported eventTime field, so StartsAt is the durable copy.
func (a alert) eventTimestamp() time.Time {
	if !a.eventTime.IsZero() {
		return a.eventTime
	}
	t, err := time.Parse(time.RFC3339Nano, a.StartsAt)
	if err != nil {
		return time.Time{}
	}
	return t
}

// tooOld reports whether the event is beyond the replay cutoff. This is
// deliberately separate from alertTTL: delivery delay must not discard an
// event that is still in the sensor's retained log, while a very old event
// should not page as if it happened now.
func (a alert) tooOld(now time.Time, maxAge time.Duration) bool {
	if maxAge <= 0 {
		return false
	}
	t := a.eventTimestamp()
	if t.IsZero() {
		return false
	}
	return now.Sub(t) > maxAge
}

func (a alert) dedupKey() string {
	keys := make([]string, 0, len(a.Labels))
	for k := range a.Labels {
		if k == "severity" {
			continue // severity changes should not defeat dedup
		}
		keys = append(keys, k+"="+a.Labels[k])
	}
	sort.Strings(keys)
	summary := normalizeDedupSummary(a.Annotations["summary"])
	sum := sha256.Sum256([]byte(strings.Join(keys, "|") + "|" + summary))
	return hex.EncodeToString(sum[:8])
}

type dedup struct {
	window time.Duration
	mu     sync.Mutex
	keys   map[string]time.Time
}

func newDedup(window time.Duration) dedup {
	return dedup{window: window, keys: map[string]time.Time{}}
}

// seen reports whether key was forwarded within the dedup window. The
// caller marks it only after the alert is durably stored, so a failed
// append stays retryable.
func (d *dedup) seen(key string) bool {
	if d.window <= 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	last, ok := d.keys[key]
	return ok && time.Since(last) < d.window
}

// mark records that key is now durable.
func (d *dedup) mark(key string) {
	if d.window <= 0 {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	// Bound memory: if the map is huge, drop expired entries first.
	if len(d.keys) > 4096 {
		for k, t := range d.keys {
			if now.Sub(t) >= d.window {
				delete(d.keys, k)
			}
		}
	}
	d.keys[key] = now
}

type alertQueue struct {
	mu    sync.Mutex
	max   int
	items []alert
}

// push appends the alert, dropping the oldest when full. It reports
// whether the new alert was kept (false means the queue rejected it).
func (q *alertQueue) push(a alert) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.max <= 0 {
		return false
	}
	if len(q.items) >= q.max {
		q.items = q.items[1:]
	}
	q.items = append(q.items, a)
	return true
}

func (q *alertQueue) pushFront(a alert) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) >= q.max {
		return false
	}
	q.items = append([]alert{a}, q.items...)
	return true
}

func (q *alertQueue) pop() (alert, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) == 0 {
		return alert{}, false
	}
	a := q.items[0]
	q.items = q.items[1:]
	return a, true
}

func (q *alertQueue) depth() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.items)
}

type metrics struct {
	forwarded    atomic.Int64
	dropped      atomic.Int64
	deduped      atomic.Int64
	expired      atomic.Int64
	gaps         atomic.Int64
	oversized    atomic.Int64
	rejected     atomic.Int64
	cursorErrors atomic.Int64
	failures     atomic.Int64

	suppressedMu sync.Mutex
	suppressed   map[string]int64
}

func (m *metrics) suppress(reason string) {
	if reason == "" {
		return
	}
	m.suppressedMu.Lock()
	defer m.suppressedMu.Unlock()
	if m.suppressed == nil {
		m.suppressed = map[string]int64{}
	}
	m.suppressed[reason]++
}

func (m *metrics) suppressedCounts() map[string]int64 {
	m.suppressedMu.Lock()
	defer m.suppressedMu.Unlock()
	out := make(map[string]int64, len(m.suppressed))
	for reason, count := range m.suppressed {
		out[reason] = count
	}
	return out
}

func newAlert(cfg config, source, severity, layer, rule, summary string) alert {
	now := time.Now().UTC()
	a := alert{
		Labels: map[string]string{
			"alertname": alertName,
			"source":    source,
			"severity":  severity,
			"host":      cfg.hostname,
			"layer":     layer,
			"rule_id":   rule,
		},
		Annotations: map[string]string{
			"summary": summary,
		},
		StartsAt:  now.Format(time.RFC3339Nano),
		eventTime: now,
	}
	redactMap(a.Annotations)
	redactMap(a.Labels)
	a.Annotations["summary"] = boundSummary(a.Annotations["summary"])
	return a
}

func lineHash(line string) string {
	sum := sha256.Sum256([]byte(line))
	return hex.EncodeToString(sum[:])
}
