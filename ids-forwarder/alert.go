package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// alert is an Alertmanager v2 alert payload.
type alert struct {
	Labels       map[string]string `json:"labels"`
	Annotations  map[string]string `json:"annotations"`
	StartsAt     string            `json:"startsAt"`
	EndsAt       string            `json:"endsAt"`
	GeneratorURL string            `json:"generatorURL,omitempty"`
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

func (d *dedup) recent(key string) bool {
	if d.window <= 0 {
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	last, ok := d.seen[key]
	return ok && time.Since(last) < d.window
}

func (d *dedup) mark(key string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	// Bound memory: cap tracked keys at 4096 by dropping expired entries.
	if len(d.seen) > 4096 {
		now := time.Now()
		for k, t := range d.seen {
			if now.Sub(t) > d.window {
				delete(d.seen, k)
			}
		}
	}
	d.seen[key] = time.Now()
}

type alertQueue struct {
	mu     sync.Mutex
	max    int
	items  []alert
	nextID int64
}

func (q *alertQueue) push(a alert) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	if len(q.items) >= q.max {
		// Drop the oldest to keep the queue bounded.
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

var ipv4Re = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
var ipv6Re = regexp.MustCompile(`\b(?:(?:[0-9a-fA-F]{1,4}:){1,7}|:)(?::[0-9a-fA-F]{1,4})+|:(?::[0-9a-fA-F]{0,4}){1,6}\b`)

// redactIPs replaces IPv4 and plain IPv6 addresses so no user IP reaches
// Alertmanager labels, annotations, or ntfy messages.
func redactIPs(s string) string {
	s = ipv4Re.ReplaceAllString(s, "[ip]")
	s = ipv6Re.ReplaceAllString(s, "[ip]")
	return s
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
		StartsAt:     now,
		EndsAt:       time.Now().Add(cfg.alertTTL).UTC().Format(time.RFC3339Nano),
		GeneratorURL: "https:// intrusion-detection.invalid",
	}
	a.GeneratorURL = ""
	redactMap(a.Annotations)
	redactMap(a.Labels)
	return a
}

// tetragonAlert maps one Tetragon JSON event line to an alert. It returns
// ok=false for events that are not configured for forwarding.
func tetragonAlert(line string, cfg config) (alert, bool) {
	var ev map[string]any
	if err := json.Unmarshal([]byte(line), &ev); err != nil {
		return alert{}, false
	}
	include := map[string]bool{}
	for _, name := range strings.Split(cfg.events, ",") {
		include[strings.TrimSpace(name)] = true
	}
	for evType := range ev {
		if evType == "time" || evType == "node_name" {
			continue
		}
		if !include[evType] {
			return alert{}, false
		}
		body, ok := ev[evType].(map[string]any)
		if !ok {
			return alert{}, false
		}
		layer := "host"
		rule := evType
		if p, _ := body["policy_name"].(string); p != "" {
			rule = p
		}
		if p, _ := ev["process_exec"].(map[string]any); p != nil {
			layer = "container"
		}
		severity := "warning"
		if msg, _ := body["message"].(string); strings.Contains(strings.ToLower(msg), "kill") {
			severity = "critical"
		}
		return newAlert(cfg, sourceTetragon, severity, layer, rule, eventSummary(evType, body)), true
	}
	return alert{}, false
}

func eventSummary(evType string, body map[string]any) string {
	var parts []string
	if p, ok := body["process"].(map[string]any); ok {
		if b, _ := p["binary"].(string); b != "" {
			parts = append(parts, "binary="+b)
		}
		if a, _ := p["arguments"].(string); a != "" {
			parts = append(parts, "args="+a)
		}
	}
	if fa, ok := body["args"].([]any); ok && len(fa) > 0 {
		if s, err := json.Marshal(fa); err == nil {
			parts = append(parts, "kprobe_args="+string(s))
		}
	}
	if msg, _ := body["message"].(string); msg != "" {
		parts = append(parts, msg)
	}
	if len(parts) == 0 {
		return fmt.Sprintf("tetragon %s event", evType)
	}
	return redactIPs(fmt.Sprintf("%s: %s", evType, strings.Join(parts, " ")))
}

// crowdsecAlert maps one CrowdSec notification-http payload item to an alert.
func crowdsecAlert(item map[string]any, cfg config) alert {
	rule, _ := item["scenario"].(string)
	if rule == "" {
		rule, _ = item["name"].(string)
	}
	if rule == "" {
		rule = "crowdsec"
	}
	severity, _ := item["severity"].(string)
	if severity == "" {
		severity = "warning"
	}
	summary, _ := item["message"].(string)
	if summary == "" {
		summary = "crowdsec alert"
	}
	return newAlert(cfg, sourceCrowdSec, severity, "host", rule, summary)
}

// tailOpen opens the file and returns its size so the caller can detect
// rotation; the tailer starts at the end of the file.
func tailOpen(path string) (*os.File, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, err
	}
	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	if _, err := f.Seek(info.Size(), 0); err != nil {
		f.Close()
		return nil, 0, err
	}
	return f, info.Size(), nil
}

// lineReader is a minimal line scanner over an *os.File that tolerates
// partial writes by buffering incomplete lines until more data arrives.
type lineReader struct {
	f   *os.File
	br  *bufio.Reader
	buf []byte
}

func newLineReader(f *os.File, buf []byte) *lineReader {
	if buf == nil {
		buf = make([]byte, 0, 64*1024)
	}
	return &lineReader{f: f, br: bufio.NewReaderSize(f, 64*1024), buf: buf[:0]}
}

func (lr *lineReader) next() ([]byte, bool) {
	line, err := lr.br.ReadBytes('\n')
	if err != nil {
		return nil, false
	}
	return line, true
}

func (lr *lineReader) close() {
	lr.f.Close()
}
