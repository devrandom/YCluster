package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"path"
	"strings"
	"time"
)

// eventMeta identifies one source line for durable replay. The hash is
// the cursor boundary; the event time is used for age filtering and gap
// reporting, never as a watermark.
type eventMeta struct {
	time time.Time
	hash string
}

func parseEventMeta(line string, env *tetragonEnvelope) eventMeta {
	meta := eventMeta{hash: lineHash(line)}
	if env != nil {
		if t, err := time.Parse(time.RFC3339Nano, env.Time); err == nil {
			meta.time = t.UTC()
		}
	}
	if meta.time.IsZero() {
		meta.time = time.Now().UTC()
	}
	return meta
}

// eventType returns the Tetragon event type name present in the line, or
// "" when the line is not a known event.
func eventType(line string) (string, *tetragonEnvelope, *tetragonLog) {
	var env tetragonEnvelope
	if err := json.Unmarshal([]byte(line), &env); err != nil {
		return "", nil, nil
	}
	if env.Exec != nil {
		return "process_exec", &env, env.Exec
	}
	if env.Kprobe != nil {
		return "process_kprobe", &env, env.Kprobe
	}
	if env.Exit != nil {
		return "process_exit", &env, &tetragonLog{Process: env.Exit.Process}
	}
	if env.Connect != nil {
		return "process_connect", &env, &tetragonLog{Process: env.Connect.Process}
	}
	return "", nil, nil
}

// tetragonAlertWithMeta maps one Tetragon JSON event line to an alert and
// its source position. ok=false covers malformed, unconfigured and
// filtered events; meta is still returned when the line is valid so the
// caller can advance its replay cursor past it.
func tetragonAlertWithMeta(line string, cfg config) (alert, eventMeta, bool) {
	a, meta, ok, _ := tetragonAlertDecision(line, cfg)
	return a, meta, ok
}

// tetragonAlertDecision is tetragonAlertWithMeta plus the routine-exec
// suppression reason, so the caller can count why an event was dropped.
func tetragonAlertDecision(line string, cfg config) (alert, eventMeta, bool, string) {
	evType, env, body := eventType(line)
	meta := parseEventMeta(line, env)
	if evType == "" || body == nil {
		return alert{}, eventMeta{}, false, ""
	}
	if !configuredEvent(cfg.events, evType) {
		return alert{}, meta, false, ""
	}
	// process_exec fires for every exec on the node; forward only the
	// binaries that matter for detection (shells, privilege tools,
	// package managers, downloaders).
	if evType == "process_exec" {
		if !configuredExecBinary(cfg.execBinaries, body.Process.Binary) {
			return alert{}, meta, false, ""
		}
		if reason := routineExecReason(body.Process); reason != "" {
			return alert{}, meta, false, reason
		}
	}
	a := newAlert(cfg, sourceTetragon, tetragonSeverity(body), tetragonLayer(body), tetragonRule(evType, body), eventSummary(evType, body))
	a.StartsAt = meta.time.Format(time.RFC3339Nano)
	a.eventTime = meta.time
	a.eventHash = meta.hash
	return a, meta, true, ""
}

// tetragonAlert is the mapping-only view used by tests and callers that do
// not need the replay cursor metadata.
func tetragonAlert(line string, cfg config) (alert, bool) {
	a, _, ok := tetragonAlertWithMeta(line, cfg)
	return a, ok
}

func configuredEvent(eventsCSV, evType string) bool {
	for _, name := range strings.Split(eventsCSV, ",") {
		if strings.TrimSpace(name) == evType {
			return true
		}
	}
	return false
}

func configuredExecBinary(binariesCSV, binary string) bool {
	base := path.Base(strings.TrimSpace(binary))
	if base == "." || base == "/" || base == "" {
		return false
	}
	for _, name := range strings.Split(binariesCSV, ",") {
		if strings.TrimSpace(name) == base {
			return true
		}
	}
	return false
}

// routineExecReason classifies known cluster probes and automation. The
// binary allowlist is intentionally broad; without this filter, health
// checks and package introspection dominate process_exec alerts.
func routineExecReason(process tetragonProcess) string {
	base := path.Base(process.Binary)
	parent := ""
	if process.Parent != nil {
		parent = process.Parent.Binary
	}
	args := process.Arguments

	switch base {
	case "curl", "wget":
		if argumentsHaveLoopbackURL(args) {
			return "loopback_http"
		}
	case "sh", "bash", "dash", "zsh", "fish":
		if parent == "/proc/self/fd/9" &&
			strings.Contains(args, "/usr/share/prometheus-node-exporter-collectors/") {
			return "node_exporter"
		}
		if healthCheckArgs(args) &&
			(parent == "/lifecycle/ak" ||
				parent == "/usr/bin/containerd-shim-runc-v2" ||
				parent == "/usr/bin/runc" ||
				parent == "/bin/sh" ||
				parent == "/bin/bash") {
			return "health_check"
		}
		if parent == "/usr/sbin/sshd" {
			if strings.Contains(args, "&& sleep 0") ||
				strings.Contains(args, "/etc/update-motd.d") {
				return "ansible_probe"
			}
		}
	case "sudo":
		if strings.Contains(args, "BECOME-SUCCESS-") {
			return "ansible_probe"
		}
	case "dpkg":
		if strings.Contains(args, "--print-foreign-architectures") {
			return "package_query"
		}
	}
	return ""
}

func healthCheckArgs(args string) bool {
	lower := strings.ToLower(args)
	return strings.Contains(lower, "/health") || strings.Contains(lower, "healthcheck")
}

func argumentsHaveLoopbackURL(args string) bool {
	for _, field := range strings.Fields(args) {
		field = strings.Trim(field, "\"'")
		u, err := url.Parse(field)
		if err != nil || u.Scheme == "" || u.Host == "" {
			continue
		}
		host := u.Hostname()
		if host == "localhost" || strings.HasSuffix(host, ".localhost") {
			return true
		}
		if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
			return true
		}
	}
	return false
}

func tetragonRule(evType string, body *tetragonLog) string {
	if body.PolicyName != "" {
		return body.PolicyName
	}
	return evType
}

// tetragonLayer reports "container" when pod metadata is attached to the
// process (or its parent), and "host" otherwise.
func tetragonLayer(body *tetragonLog) string {
	if body.Process.Pod != nil {
		return "container"
	}
	if body.Process.Parent != nil && body.Process.Parent.Pod != nil {
		return "container"
	}
	return "host"
}

func tetragonSeverity(body *tetragonLog) string {
	if strings.Contains(strings.ToLower(body.Message), "kill") {
		return "critical"
	}
	return "warning"
}

func eventSummary(evType string, body *tetragonLog) string {
	parts := make([]string, 0, 3)
	if body.Process.Binary != "" {
		parts = append(parts, "binary="+body.Process.Binary)
	}
	if body.Process.Arguments != "" {
		parts = append(parts, "args="+body.Process.Arguments)
	}
	if len(body.Args) > 0 && string(body.Args) != "null" {
		parts = append(parts, "kprobe_args="+string(body.Args))
	}
	if body.Message != "" {
		parts = append(parts, body.Message)
	}
	if len(parts) == 0 {
		return fmt.Sprintf("tetragon %s event", evType)
	}
	return redactIPs(fmt.Sprintf("%s: %s", evType, strings.Join(parts, " ")))
}

// crowdsecToAlert maps one CrowdSec notification-http payload item to an
// alert. The HTTP plugin posts a list of models.Alert objects; the fields
// below follow CrowdSec 1.8.x pkg/models.Alert. CrowdSec alerts carry no
// severity field, so the fixed "warning" is used.
func crowdsecToAlert(item crowdsecAlert, cfg config) alert {
	rule := item.Scenario
	if rule == "" {
		rule = "crowdsec"
	}
	summary := rule
	var parts []string
	if item.Machine != nil && item.Machine.Hostname != "" {
		parts = append(parts, "machine="+item.Machine.Hostname)
	}
	if item.Source != nil {
		// No address or range in the summary: a /32 range is an exact IP,
		// and redaction of untrusted free text is best-effort. The source
		// presence itself is the triage signal.
		parts = append(parts, "source=present")
	}
	if len(item.Events) > 0 {
		parts = append(parts, fmt.Sprintf("events=%d", len(item.Events)))
		if item.Events[0].Message != "" {
			summary = item.Events[0].Message
		}
	}
	if len(parts) > 0 {
		summary = summary + " (" + strings.Join(parts, " ") + ")"
	}
	return newAlert(cfg, sourceCrowdSec, "warning", "host", rule, summary)
}
