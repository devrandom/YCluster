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
	// dedupIdentity carries the part of an event that the visible summary
	// redacts, so two destinations that both print as [ip] still get
	// different dedup keys. Never serialised.
	dedupIdentity string
	// prov records how the layer and escalation verdicts were reached. It is
	// counted at decision time and never serialised.
	prov eventProvenance
}

// eventProvenance names the source of each classification verdict, so the
// metric can show that the event-carried sources dominate and the /proc
// fallbacks are rare outside replay.
type eventProvenance struct {
	layer      string
	escalation string
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
	shell := a.Labels["rule_id"] == "process_exec" && isShellBinary(a.Labels["binary"])
	keys := make([]string, 0, len(a.Labels))
	for k := range a.Labels {
		if k == "origin" || k == "actor" {
			// Provenance describes the alert, it does not make it a
			// different signal: one shell on one host stays one alert
			// whichever session or unit ran it.
			continue
		}
		if k == "binary" && shell {
			continue // sh, bash and dash are the same signal on one host
		}
		keys = append(keys, k+"="+a.Labels[k])
	}
	sort.Strings(keys)
	summary := normalizeDedupSummary(a.Annotations["summary"])
	if shell {
		// A shell on the host is one signal: someone (or something) with
		// root has a shell. Colour that by the first command line per
		// window instead of once per command; every command is still
		// recorded in detections.jsonl.
		summary = ""
	}
	switch a.Labels["rule_id"] {
	case "container-api", "container-exec":
		// Per-command detail belongs in the record: one API-usage alert per
		// host per window is enough to notice that someone is driving the
		// Incus API, and it keeps an interactive session from paging once
		// per subcommand. Forkfile carries the invocation's pids, so its
		// summary would otherwise defeat the window as well.
		summary = ""
	}
	sum := sha256.Sum256([]byte(strings.Join(keys, "|") + "|" + summary + "|" + a.dedupIdentity))
	return hex.EncodeToString(sum[:8])
}

type dedup struct {
	window time.Duration
	// retain is how long a key stays in the map. It must cover the longest
	// per-rule repeat window, not just the shared one, or a busy host
	// evicts the entries the hourly coalescing depends on.
	retain time.Duration
	mu     sync.Mutex
	keys   map[string]time.Time
}

func newDedup(window time.Duration) dedup {
	return dedup{window: window, retain: dedupRetention(window), keys: map[string]time.Time{}}
}

// dedupRetention is the shared window widened to the longest repeat window.
func dedupRetention(window time.Duration) time.Duration {
	retain := window
	for _, w := range ruleRepeatWindows {
		if w > retain {
			retain = w
		}
	}
	return retain
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

// seenWithin reports whether key was seen within an arbitrary window. Rules
// that describe a recurring action rather than a new condition use a window
// longer than the shared dedup window.
func (d *dedup) seenWithin(key string, window time.Duration) bool {
	if window <= 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	last, ok := d.keys[key]
	return ok && time.Since(last) < window
}

// mark records that key is now durable.
func (d *dedup) mark(key string) {
	if d.window <= 0 {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	// Bound memory: if the map is huge, drop entries past the retention
	// window first. Pruning by d.window here would silently shorten the
	// per-rule coalescing windows.
	if len(d.keys) > 4096 {
		for k, t := range d.keys {
			if now.Sub(t) >= d.retain {
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
	forwarded       atomic.Int64
	dropped         atomic.Int64
	deduped         atomic.Int64
	coalesced       atomic.Int64
	bootSuppressed  atomic.Int64
	rateLimited     atomic.Int64
	expired         atomic.Int64
	gaps            atomic.Int64
	oversized       atomic.Int64
	rejected        atomic.Int64
	cursorErrors    atomic.Int64
	failures        atomic.Int64
	partialDelivery atomic.Int64
	recorded        atomic.Int64
	recordErrors    atomic.Int64
	sigmaBad        atomic.Int64
	sigmaTailErrors atomic.Int64

	suppressedMu sync.Mutex
	suppressed   map[string]int64

	originMu sync.Mutex
	origins  map[string]int64

	classMu  sync.Mutex
	evidence map[string]int64
	paged    map[string]int64

	provMu sync.Mutex
	prov   map[string]int64
}

// countProvenance tallies how a classification verdict was reached.
func (m *metrics) countProvenance(p eventProvenance) {
	m.provMu.Lock()
	defer m.provMu.Unlock()
	if m.prov == nil {
		m.prov = map[string]int64{}
	}
	if p.layer != "" {
		m.prov["layer\x00"+p.layer]++
	}
	if p.escalation != "" {
		m.prov["escalation\x00"+p.escalation]++
	}
}

func (m *metrics) provenanceCounts() map[string]int64 {
	m.provMu.Lock()
	defer m.provMu.Unlock()
	out := make(map[string]int64, len(m.prov))
	for key, count := range m.prov {
		out[key] = count
	}
	return out
}

// countEvidence tallies detections dropped as post-root evidence, by class.
// The count is what shows how much of the stream the model now keeps out of
// the paging path.
func (m *metrics) countEvidence(class string) {
	m.classMu.Lock()
	defer m.classMu.Unlock()
	if m.evidence == nil {
		m.evidence = map[string]int64{}
	}
	m.evidence[class]++
}

func (m *metrics) evidenceCounts() map[string]int64 {
	m.classMu.Lock()
	defer m.classMu.Unlock()
	out := make(map[string]int64, len(m.evidence))
	for class, count := range m.evidence {
		out[class] = count
	}
	return out
}

// countPageCandidate tallies a delivered-but-silent coverage detection, after
// dedup and coalescing, at the point where enabling its class would have
// paged. The measurement gate reads this instead of running a class live.
func (m *metrics) countPageCandidate(class string) {
	m.classMu.Lock()
	defer m.classMu.Unlock()
	if m.paged == nil {
		m.paged = map[string]int64{}
	}
	m.paged[class]++
}

func (m *metrics) pageCandidateCounts() map[string]int64 {
	m.classMu.Lock()
	defer m.classMu.Unlock()
	out := make(map[string]int64, len(m.paged))
	for class, count := range m.paged {
		out[class] = count
	}
	return out
}

// countOrigin tallies classified detections by provenance, before dedup,
// coalescing and rate limiting, so the origin split can be measured before
// any routing depends on it. An empty origin is counted as "unset" rather
// than skipped, so a labelling regression is visible instead of silent.
func (m *metrics) countOrigin(origin string) {
	if origin == "" {
		origin = "unset"
	}
	m.originMu.Lock()
	defer m.originMu.Unlock()
	if m.origins == nil {
		m.origins = map[string]int64{}
	}
	m.origins[origin]++
}

func (m *metrics) originCounts() map[string]int64 {
	m.originMu.Lock()
	defer m.originMu.Unlock()
	out := make(map[string]int64, len(m.origins))
	for origin, count := range m.origins {
		out[origin] = count
	}
	return out
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
	a.Annotations["summary"] = boundSummary(redactSecrets(a.Annotations["summary"]))
	return a
}

// identityHash is a stable, non-reversible marker for the part of an event
// that the visible summary may redact. It never leaves the process.
func identityHash(s string) string {
	if s == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:8])
}

func lineHash(line string) string {
	sum := sha256.Sum256([]byte(line))
	return hex.EncodeToString(sum[:])
}
