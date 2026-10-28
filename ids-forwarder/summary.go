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
	// Ansible creates one temp directory per task from the clock, the pid
	// and a random suffix. Without this, every task is a new signature.
	{regexp.MustCompile(`ansible-tmp-[0-9.]+-[0-9]+-[0-9]+`), "ansible-tmp-*"},
	// UUIDs appear in socket paths and state directories.
	{regexp.MustCompile(`[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`), "<uuid>"},
	// Editors sample CPU usage per pid, so the arguments churn every run.
	{regexp.MustCompile(`cpuUsage\.sh"?\s+[0-9 ]+`), "cpuUsage.sh <pids>"},
	// The VS Code server picks a new bridge port per launch.
	{regexp.MustCompile(`--agent-host-bridge-port=[0-9]+`), "--agent-host-bridge-port=<port>"},
	// Its shell-integration probe injects a random token into the command.
	{regexp.MustCompile(`"\s*[0-9a-f]{8,16}" \+ JSON\.stringify\(process\.env\)`), `"<token>" + JSON.stringify(process.env)`},
}

// secretPatterns are replaced in the visible summary as well as the dedup
// key: these values are credentials, not identifiers, and must not be
// copied into Alertmanager, detections.jsonl or a notification.
var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`--connection-token=\S+`),
	regexp.MustCompile(`--connection-token \S+`),
}

// ipmiPasswordPattern covers ipmitool's -P flag, which carries the BMC
// password on the command line.
var ipmiPasswordPattern = regexp.MustCompile(`(ipmitool\b[^\n]*?\s-P\s+)("[^"]*"|'[^']*'|\S+)`)

// base64DecodePattern covers an inline decoded literal. Wrapping the secret
// in base64 keeps it out of shell history but the decoder's own argv still
// carries the encoded value.
var base64DecodePattern = regexp.MustCompile(`(base64\s+(?:-d|--decode)\s*<<<\s*)("[^"]*"|'[^']*'|\S+)`)

func redactSecrets(summary string) string {
	for _, re := range secretPatterns {
		summary = re.ReplaceAllStringFunc(summary, func(m string) string {
			switch {
			case strings.HasPrefix(m, "--connection-token="):
				return "--connection-token=<redacted>"
			default:
				return "--connection-token <redacted>"
			}
		})
	}
	summary = ipmiPasswordPattern.ReplaceAllString(summary, `${1}<redacted>`)
	summary = base64DecodePattern.ReplaceAllString(summary, `${1}<redacted>`)
	return summary
}

// socketArgVolatile strips per-connection fields from serialized Tetragon
// socket arguments. The source port, socket cookie and TCP state differ for
// every connect, so keeping them would give each event a unique summary and
// defeat deduplication; only the destination identifies the event.
var socketArgVolatile = []*regexp.Regexp{
	regexp.MustCompile(`"sport":\d+,\s*`),
	regexp.MustCompile(`"cookie":"\d+",\s*`),
}

func normalizeSocketArgs(raw string) string {
	for _, re := range socketArgVolatile {
		raw = re.ReplaceAllString(raw, "")
	}
	return raw
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
