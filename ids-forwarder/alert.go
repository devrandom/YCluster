package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/netip"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// alert is an Alertmanager v2 alert payload.
type alert struct {
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
	StartsAt    string            `json:"startsAt"`
	EndsAt      string            `json:"endsAt"`
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
	sum := sha256.Sum256([]byte(strings.Join(keys, "|") + "|" + a.Annotations["summary"]))
	return hex.EncodeToString(sum[:8])
}

type dedup struct {
	window time.Duration
	mu     sync.Mutex
	seen   map[string]time.Time
}

func newDedup(window time.Duration) dedup {
	return dedup{window: window, seen: map[string]time.Time{}}
}

// seenAndMark atomically checks whether the key was forwarded within the
// dedup window and records it. A single operation prevents duplicate
// forwards when events arrive concurrently.
func (d *dedup) seenAndMark(key string) bool {
	if d.window <= 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	if last, ok := d.seen[key]; ok && now.Sub(last) < d.window {
		return true
	}
	// Bound memory: if the map is huge, drop expired entries first.
	if len(d.seen) > 4096 {
		for k, t := range d.seen {
			if now.Sub(t) >= d.window {
				delete(d.seen, k)
			}
		}
	}
	d.seen[key] = now
	return false
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
	forwarded atomic.Int64
	dropped   atomic.Int64
	deduped   atomic.Int64
	failures  atomic.Int64
}

// redactIPs replaces IPv4 and IPv6 addresses so no user IP reaches
// Alertmanager labels, annotations, or ntfy messages. Tokens are split on
// characters that cannot appear in an address, validated with
// netip.ParseAddr, and replaced individually.
func redactIPs(s string) string {
	if !strings.ContainsAny(s, ".:") {
		return s
	}
	var b strings.Builder
	for _, tok := range splitKeep(s, isSeparator) {
		b.WriteString(redactToken(tok))
	}
	return b.String()
}

func isSeparator(r rune) bool {
	switch r {
	case ' ', '\t', '\n', ',', ';', '"', '\'', '(', ')', '{', '}', '=', '<', '>', '|':
		return true
	}
	return false
}

func redactToken(tok string) string {
	if scheme, rest, ok := strings.Cut(tok, "://"); ok && isScheme(scheme) {
		return redactURL(scheme, rest, tok)
	}
	if redacted, ok := redactBracketed(tok); ok {
		return redacted
	}
	trim := strings.Trim(tok, "[]")
	if trim == "" {
		return tok
	}
	if redacted, ok := redactCIDR(trim, tok); ok {
		return redacted
	}
	if redacted, ok := redactHostPort(trim, tok); ok {
		return redacted
	}
	if isAddr(trim) {
		return "[ip]"
	}
	return tok
}

// redactURL redacts the host of a scheme://host/... token.
func redactURL(scheme, rest, tok string) string {
	if host, trail, ok := cutHost(rest); ok {
		if redacted := redactToken(host); redacted != host {
			return scheme + "://" + redacted + trail
		}
	}
	return tok
}

// redactBracketed handles "[addr]...": bracketed IPv6 with an optional
// port or trailing path. Returns ok=false when the token is not bracketed
// or the bracket contents are not an address.
func redactBracketed(tok string) (string, bool) {
	if !strings.HasPrefix(tok, "[") {
		return "", false
	}
	closeIdx := strings.Index(tok, "]")
	if closeIdx < 1 {
		return "", false
	}
	inner := tok[1:closeIdx]
	if _, err := netip.ParseAddr(inner); err != nil {
		return "", false
	}
	return "[ip]" + tok[closeIdx+1:], true
}

// redactCIDR handles "addr/len" tokens, keeping the prefix length.
func redactCIDR(trim, tok string) (string, bool) {
	host, suffix, ok := strings.Cut(trim, "/")
	if !ok || suffix == "" {
		return "", false
	}
	if _, err := netip.ParseAddr(host); err != nil || !isPortOrLen(suffix) {
		return "", false
	}
	return "[ip]/" + suffix, true
}

// redactHostPort handles "ipv4:port" tokens, keeping the port.
func redactHostPort(trim, tok string) (string, bool) {
	host, port, ok := strings.Cut(trim, ":")
	if !ok || strings.Contains(host, ":") {
		return "", false
	}
	ip, err := netip.ParseAddr(host)
	if err != nil || !ip.Is4() || !isPort(port) {
		return "", false
	}
	return "[ip]:" + port, true
}

func isAddr(s string) bool {
	_, err := netip.ParseAddr(s)
	return err == nil
}

func isScheme(s string) bool {
	switch strings.ToLower(s) {
	case "http", "https", "ws", "wss":
		return true
	}
	return false
}

// cutHost splits "host[:port][/path]" into host and the remainder.
func cutHost(rest string) (string, string, bool) {
	if strings.HasPrefix(rest, "[") {
		closeIdx := strings.Index(rest, "]")
		if closeIdx < 0 {
			return "", "", false
		}
		return rest[:closeIdx+1], rest[closeIdx+1:], true
	}
	end := strings.IndexAny(rest, "/:")
	if end < 0 {
		return rest, "", true
	}
	return rest[:end], rest[end:], true
}

func isPort(s string) bool {
	if s == "" || len(s) > 5 {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// isPortOrLen reports whether s is a valid TCP/UDP port or IP prefix
// length (0-128, digits only, at most 5 chars). Used to decide whether a
// "/suffix" after an address is a prefix length worth preserving.
func isPortOrLen(s string) bool {
	if !isPort(s) {
		return false
	}
	n, err := strconv.Atoi(s)
	return err == nil && n >= 0 && n <= 128
}

func redactMap(m map[string]string) {
	for k, v := range m {
		m[k] = redactIPs(v)
	}
}

func newAlert(cfg config, source, severity, layer, rule, summary string) alert {
	now := time.Now().UTC().Format(time.RFC3339Nano)
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
		StartsAt: now,
		EndsAt:   time.Now().Add(cfg.alertTTL).UTC().Format(time.RFC3339Nano),
	}
	redactMap(a.Annotations)
	redactMap(a.Labels)
	return a
}

// splitKeep splits s on the rune class but keeps the separators as their
// own tokens so rejoining reconstructs the original string.
func splitKeep(s string, sep func(rune) bool) []string {
	var out []string
	start := 0
	for i, r := range s {
		if sep(r) {
			if i > start {
				out = append(out, s[start:i])
			}
			out = append(out, string(r))
			start = i + len(string(r))
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
