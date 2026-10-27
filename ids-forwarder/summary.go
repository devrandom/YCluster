package main

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

const maxSummaryBytes = 4096

var dedupVolatilePatterns = []struct {
	re   *regexp.Regexp
	repl string
}{
	{regexp.MustCompile(`BECOME-SUCCESS-[A-Za-z0-9]+`), "BECOME-SUCCESS-*"},
	{regexp.MustCompile(`/tmp/runc-process[0-9]+`), "/tmp/runc-process*"},
	{regexp.MustCompile(`/etc/prometheus/tmp[A-Za-z0-9_]+`), "/etc/prometheus/tmp*"},
	{regexp.MustCompile(`/etc/zulip/push-hosts\.[A-Za-z0-9]+`), "/etc/zulip/push-hosts.*"},
	{regexp.MustCompile(`(?i)\b[0-9a-f]{32,64}\b`), "<hex-id>"},
}

// normalizeDedupSummary removes volatile identifiers from the dedup key
// while leaving the alert's visible summary intact for triage.
func normalizeDedupSummary(summary string) string {
	for _, p := range dedupVolatilePatterns {
		summary = p.re.ReplaceAllString(summary, p.repl)
	}
	return strings.Join(strings.Fields(summary), " ")
}

// boundSummary caps an annotation so a single event cannot inflate the
// Alertmanager payload and durable spool without adding useful evidence.
func boundSummary(summary string) string {
	if len(summary) <= maxSummaryBytes {
		return summary
	}
	cut := maxSummaryBytes - len("...")
	for cut > 0 && !utf8.RuneStart(summary[cut]) {
		cut--
	}
	return summary[:cut] + "..."
}
