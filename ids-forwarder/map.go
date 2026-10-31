package main

import (
	"encoding/json"
	"fmt"
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
	// Root executing a payload staged in a user-writable tree crosses the
	// privilege boundary without needing a package-manager shell. Containers
	// are excluded: a root process inside a tenant container sees its own
	// filesystem, not the host's.
	if evType == "process_exec" && cfg.execRootUserPath && body.Process.UID == 0 {
		if layer, _ := tetragonEventLayer(body); layer == "host" && rootExecUserPath(body.Process.Binary) {
			a := newAlert(cfg, sourceTetragon, "warning", layer, "exec-root-user-path",
				"process_exec: binary="+body.Process.Binary+" root-exec")
			a.Labels["binary"] = processBinaryName(body.Process.Binary)
			a.dedupIdentity = identityHash("exec-root-user-path|" + body.Process.Binary)
			a.StartsAt = meta.time.Format(time.RFC3339Nano)
			a.eventTime = meta.time
			a.eventHash = meta.hash
			return a, meta, true, ""
		}
	}
	// process_exec fires for every exec on the node; forward only the
	// binaries that matter for detection (shells, privilege tools,
	// package managers, downloaders).
	if evType == "process_exec" && !configuredExecBinary(cfg.execBinaries, body.Process.Binary) {
		return alert{}, meta, false, ""
	}
	layer, containerName := tetragonEventLayer(body)
	if reason := tetragonSuppression(evType, body, layer); reason != "" {
		return alert{}, meta, false, reason
	}
	summary := eventSummary(evType, body)
	a := newAlert(cfg, sourceTetragon, tetragonSeverity(body), layer, tetragonRule(evType, body), summary)
	if body.FunctionName == "tcp_connect" {
		if addr, port, ok := tcpDestination(body.Args); ok {
			a.Labels["dest"] = destinationLabel(addr, port)
		}
	}
	if evType == "process_exec" {
		// Kept as a label so the dedup key can tell a shell from a
		// package-manager or downloader exec, and so triage sees which
		// binary produced the alert.
		a.Labels["binary"] = processBinaryName(body.Process.Binary)
	}
	if containerName != "" {
		a.Labels["container"] = containerName
	}
	// The visible summary redacts addresses, so two connects to different
	// hosts would otherwise share one dedup key. Hash the normalised but
	// unredacted summary for the key; shells and API calls coalesce by
	// design and keep an empty identity.
	if a.Labels["rule_id"] != "container-api" && a.Labels["rule_id"] != "container-exec" &&
		!(evType == "process_exec" && isShellBinary(a.Labels["binary"])) {
		a.dedupIdentity = identityHash(normalizeDedupSummary(eventSummaryRaw(evType, body)))
	}
	a.StartsAt = meta.time.Format(time.RFC3339Nano)
	a.eventTime = meta.time
	a.eventHash = meta.hash
	return a, meta, true, ""
}

// tetragonEventLayer resolves the host/container layer of an event and, for
// host-attributed events, the container that owns the process.
func tetragonEventLayer(body *tetragonLog) (layer, containerName string) {
	layer = tetragonLayer(body)
	if layer != "host" {
		return layer, ""
	}
	// The host-side exec rules come after the layer check so a container's
	// shells are attributed to the container rather than matched against
	// host daemon shapes.
	if name, ok := containerForEvent(body); ok {
		return "container", name
	}
	return layer, ""
}

// tetragonSuppression returns the reason an event is not forwarded, or ""
// when it should produce an alert.
func tetragonSuppression(evType string, body *tetragonLog, layer string) string {
	if evType == "process_exec" {
		if layer == "container" {
			return "container_internal"
		}
		if reason := routineExecReason(body.Process, body.Parent); reason != "" {
			return reason
		}
	}
	if reason := packageMaintenanceReason(body); reason != "" {
		return reason
	}
	if layer == "container" {
		return containerSuppressionReason(body)
	}
	return hostSuppressionReason(body)
}

// containerSuppressionReason keeps only the container case that matters to
// the cluster: a connection leaving for a cluster-private address. Public
// egress and host-local bridge/loopback traffic are routine tenant traffic.
func containerSuppressionReason(body *tetragonLog) string {
	if reason := containerEgressReason(body, "container"); reason != "" {
		return reason
	}
	if reason := containerProbeReason(body); reason != "" {
		return reason
	}
	if body.FunctionName != "tcp_connect" {
		return "container_internal"
	}
	return ""
}

func hostSuppressionReason(body *tetragonLog) string {
	if reason := expectedTCPReason(body); reason != "" {
		return reason
	}
	if reason := routinePrivilegeReason(body); reason != "" {
		return reason
	}
	return routineFileWriteReason(body)
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
	base := processBinaryName(strings.TrimSpace(binary))
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

// routinePrivilegeReason covers process binaries whose privilege transitions
// are their normal operation. systemd's transient re-exec presents as
// /proc/self/fd/9, which matchBinaries cannot resolve, so it is handled here;
// sshd and cron transitions are filtered in the policy instead. Container
// runtimes change credentials every time they start a container, and the PCP
// collector drops to the metric user on each sample.
func routinePrivilegeReason(body *tetragonLog) string {
	if body == nil || body.PolicyName != "privilege-raises" {
		return ""
	}
	if !strings.HasPrefix(body.FunctionName, "__sys_set") &&
		body.FunctionName != "security_capset" {
		return ""
	}
	switch body.Process.Binary {
	case "/proc/self/fd/9", "/usr/lib/systemd/systemd", "/lib/systemd/systemd":
		return "expected_privilege"
	case "/usr/bin/sudo":
		// Ansible's become wrapper raises privileges for every task.
		if strings.Contains(body.Process.Arguments, "BECOME-SUCCESS-") &&
			strings.Contains(body.Process.Arguments, "/usr/bin/python3") {
			return "ansible_probe"
		}
		// sudo is setuid root and calls setresuid(-1, 0, -1) on itself
		// before dropping to its target. The policy matches that no-op as
		// a transition to uid 0, so a drop from root to a named non-root
		// user is classified here. The exec of sudo is still forwarded.
		if body.Process.UID == 0 && sudoTargetsNonRoot(body.Process.Arguments) {
			return "privilege_drop"
		}
	case "/usr/sbin/logrotate", "/usr/bin/logrotate":
		return "expected_privilege"
	case "/usr/bin/runc",
		"/usr/bin/containerd-shim-runc-v2",
		"/usr/bin/docker-proxy",
		"/usr/libexec/incus/incusd",
		"/usr/libexec/virtiofsd",
		"/usr/libexec/podman/rootlessport",
		"/usr/lib/pcp/pmdas/proc/pmdaproc",
		"/usr/sbin/smbd":
		return "expected_privilege"
	}
	// snap-confine sets up the mount namespace and capabilities for every
	// confined application start; its version directory changes on update.
	if strings.HasPrefix(body.Process.Binary, "/snap/snapd/") &&
		strings.HasSuffix(body.Process.Binary, "/usr/lib/snapd/snap-confine") {
		return "expected_privilege"
	}
	return ""
}

// packageMaintenanceReason classifies apt's own maintenance shapes, and
// only those: installs, removals and dpkg file installs stay visible.
func packageMaintenanceReason(body *tetragonLog) string {
	if body == nil {
		return ""
	}
	base := path.Base(body.Process.Binary)
	args := body.Process.Arguments
	switch base {
	case "apt-get", "apt":
		// Match whole subcommand tokens so installing a package whose name
		// contains "update" is not mistaken for a refresh.
		fields := strings.Fields(args)
		for i, f := range fields {
			switch f {
			case "update", "check":
				return "package_update"
			case "list":
				if i+1 < len(fields) && strings.Contains(fields[i+1], "upgradable") {
					return "package_update"
				}
			}
		}
	case "dpkg":
		if strings.HasPrefix(strings.TrimSpace(args), "-l ") {
			return "package_update"
		}
	case "sh", "bash", "dash":
		if strings.Contains(args, "apt-news.service esm-cache.service") ||
			strings.Contains(args, "apt/periodic/update-success-stamp") {
			return "package_update"
		}
	}
	if strings.Contains(args, "/usr/lib/ubuntu-advantage/esm_cache.py") {
		return "package_update"
	}
	return ""
}

// sudoTargetsNonRoot reports whether a sudo command line switches to a
// named user other than root. It understands -u user, --user user,
// --user=user, -uuser and clustered short flags such as -iu user.
func sudoTargetsNonRoot(args string) bool {
	fields := strings.Fields(args)
	isRoot := func(v string) bool {
		v = strings.Trim(v, "\"'")
		return v == "0" || v == "root"
	}
	for i, f := range fields {
		next := ""
		if i+1 < len(fields) {
			next = fields[i+1]
		}
		switch {
		case f == "-u" || f == "--user":
			return next != "" && !isRoot(next)
		case strings.HasPrefix(f, "--user="):
			return !isRoot(strings.TrimPrefix(f, "--user="))
		case strings.HasPrefix(f, "-") && !strings.HasPrefix(f, "--"):
			at := strings.IndexByte(f, 'u')
			if at < 0 {
				continue
			}
			if inline := f[at+1:]; inline != "" {
				return !isRoot(inline)
			}
			return next != "" && !isRoot(next)
		}
	}
	return false
}

func tetragonRule(evType string, body *tetragonLog) string {
	// The incus CLI is how an operator or a script reaches a tenant
	// instance through the API (exec, file push/pull, start). Record it
	// under its own rule so the caller's subcommand and target are visible.
	if isIncusClient(body) {
		return "container-api"
	}
	// incusd's forkfile helper creates a process inside a container on
	// behalf of the API (incus exec, file push/pull, instance start). The
	// privilege policy matches it, but it is a runtime mechanism, not an
	// escalation, so it gets its own rule id for triage.
	if isIncusForkfile(body) {
		return "container-exec"
	}
	if body.PolicyName != "" {
		return body.PolicyName
	}
	return evType
}

// processBinaryName is the binary name Tetragon reports, without the
// " (deleted)" suffix left behind by an in-place update.
func processBinaryName(binary string) string {
	return path.Base(strings.TrimSuffix(binary, " (deleted)"))
}

// isShellBinary reports whether the exec is an interactive shell, the class
// that coalesces to one alert per host per window.
func isShellBinary(name string) bool {
	switch name {
	case "sh", "bash", "dash", "zsh", "fish":
		return true
	}
	return false
}

func isIncusClient(body *tetragonLog) bool {
	if body == nil || body.PolicyName == "privilege-raises" {
		return false
	}
	return path.Base(body.Process.Binary) == "incus"
}

func isIncusForkfile(body *tetragonLog) bool {
	if body == nil || body.Process.Binary == "" {
		return false
	}
	return strings.HasSuffix(body.Process.Binary, "/incusd") &&
		strings.HasPrefix(body.Process.Arguments, "forkfile")
}

// tetragonLayer reports "container" when pod metadata is attached to the
// process or its parent. Incus containers carry no pod metadata, so callers
// fall back to the /proc cgroup lookup in containerForProcess.
func tetragonLayer(body *tetragonLog) string {
	if body == nil {
		return "host"
	}
	if body.Process.Pod != nil {
		return "container"
	}
	if body.Parent != nil && body.Parent.Pod != nil {
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
	return redactIPs(eventSummaryRaw(evType, body))
}

// eventSummaryRaw builds the summary without address redaction. It exists so
// the dedup identity can distinguish events the visible summary collapses to
// [ip]; the result is hashed and never stored or delivered.
func eventSummaryRaw(evType string, body *tetragonLog) string {
	parts := make([]string, 0, 3)
	if body.Process.Binary != "" {
		parts = append(parts, "binary="+body.Process.Binary)
	}
	if body.Process.Arguments != "" {
		parts = append(parts, "args="+body.Process.Arguments)
	}
	if len(body.Args) > 0 && string(body.Args) != "null" {
		parts = append(parts, "kprobe_args="+normalizeSocketArgs(string(body.Args)))
	}
	if body.Message != "" {
		parts = append(parts, body.Message)
	}
	if len(parts) == 0 {
		return fmt.Sprintf("tetragon %s event", evType)
	}
	return fmt.Sprintf("%s: %s", evType, strings.Join(parts, " "))
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
