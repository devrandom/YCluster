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
	if reason := packageMaintenanceReason(body); reason != "" {
		return alert{}, meta, false, reason
	}
	if reason := expectedTCPReason(body); reason != "" {
		return alert{}, meta, false, reason
	}
	if reason := routinePrivilegeReason(body); reason != "" {
		return alert{}, meta, false, reason
	}
	if reason := routineFileWriteReason(body); reason != "" {
		return alert{}, meta, false, reason
	}
	layer := tetragonLayer(body)
	containerName := ""
	if layer == "host" {
		if name, ok := containerForProcess(body.Process.PID, body.Process.StartTime); ok {
			layer = "container"
			containerName = name
		}
	}
	if reason := containerEgressReason(body, layer); reason != "" {
		return alert{}, meta, false, reason
	}
	a := newAlert(cfg, sourceTetragon, tetragonSeverity(body), layer, tetragonRule(evType, body), eventSummary(evType, body))
	if containerName != "" {
		a.Labels["container"] = containerName
	}
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
	case "incus":
		// vm-gpu-collector: on vfio-pci hosts the GPUs are invisible to
		// the host, so it lists VMs and execs nvidia-smi inside each one
		// every 30s. Known unit, known command line.
		if strings.Contains(args, "type=virtual-machine") &&
			strings.Contains(args, "list") {
			return "gpu_collector"
		}
		if strings.Contains(args, "-- nvidia-smi --query-gpu=") &&
			strings.Contains(args, "--format=csv") {
			return "gpu_collector"
		}
		// sync_transit_nics reads instance metadata and the bastion's
		// /etc/hosts on every reconciler tick to rewrite the transit
		// block. Reads only: a push to the same path still alerts.
		if strings.HasPrefix(args, "file pull ") &&
			strings.HasSuffix(strings.TrimSpace(args), "/etc/hosts -") {
			return "nat_sync"
		}
		if strings.HasPrefix(args, "info ") {
			return "nat_sync"
		}
	case "sh", "bash", "dash", "zsh", "fish":
		if strings.Contains(args, "/usr/share/prometheus-node-exporter-collectors/") {
			return "node_exporter"
		}
		if strings.Contains(args, "/etc/update-motd.d") {
			return "motd"
		}
		if zulipCronReason(args) != "" {
			return "app_cron"
		}
		// sysstat's daily-activity collector runs from cron every ten
		// minutes on every node with a fixed command line.
		if strings.Contains(args, "debian-sa1") {
			return "sysstat"
		}
		// Debian's cron.d wrappers run through this exact shell command.
		if strings.Contains(args, "/etc/cron.") {
			return "cron"
		}
		// Ansible's become handshake and its connectivity/interpreter
		// probes run on every task; they are fixed shell shapes, not
		// workload execs.
		if strings.Contains(args, "BECOME-SUCCESS-") ||
			strings.Contains(args, "echo ~ && sleep 0") ||
			(strings.Contains(args, "echo PLATFORM") && strings.Contains(args, "command -v")) ||
			(strings.Contains(args, "python3.") && strings.Contains(args, "&& sleep 0")) {
			return "ansible_probe"
		}
		// The storage health probe runs the same mountpoint check from a
		// shell wrapper on a short timer.
		trimmed := strings.TrimSpace(args)
		if strings.HasSuffix(trimmed, `-c "mountpoint -q /rbd/user"`) ||
			strings.HasSuffix(trimmed, `-c 'mountpoint -q /rbd/user'`) {
			return "storage_probe"
		}
		// The NAS mount retry uses a read-only mountpoint probe as its
		// ExecCondition, and starts the mount unit when it is missing.
		if strings.Contains(args, "mountpoint -q /mnt/nas-vm") ||
			strings.Contains(args, "mountpoint -q /near2") {
			return "mount_probe"
		}
		if strings.Contains(args, `systemctl start "`) && strings.Contains(args, `.mount"`) {
			return "mount_retry"
		}
		// Containers use a literal false command as a liveness placeholder.
		if strings.TrimSpace(args) == "-c -- /bin/false" {
			return "health_check"
		}
		// Ansible runs its file transfer through a shell wrapper around
		// sftp-server.
		if strings.Contains(args, "/usr/lib/openssh/sftp-server") {
			return "ansible_probe"
		}
		// Ansible's per-task temp directory is created, chmodded and
		// removed with the same fixed command shapes.
		if strings.Contains(args, "/root/.ansible/tmp/") {
			return "ansible_probe"
		}
		// Container health checks run without a stable parent: the shim,
		// tini or the workload itself forks them, so match the probe shape
		// instead of the parent binary.
		if healthCheckArgs(args) &&
			(strings.Contains(args, "localhost") ||
				strings.Contains(args, "127.0.0.1") ||
				strings.Contains(args, "/lifecycle/")) {
			return "health_check"
		}
		if healthCheckArgs(args) &&
			(parent == "/lifecycle/ak" ||
				parent == "/usr/bin/containerd-shim-runc-v2" ||
				parent == "/usr/bin/runc" ||
				parent == "/bin/sh" ||
				parent == "/bin/bash") {
			return "health_check"
		}
		if parent == "/usr/sbin/sshd" && strings.Contains(args, "&& sleep 0") {
			return "ansible_probe"
		}
	case "sudo":
		if strings.Contains(args, "BECOME-SUCCESS-") {
			return "ansible_probe"
		}
	case "dpkg":
		if strings.Contains(args, "--print-foreign-architectures") ||
			strings.Contains(args, "--compare-versions") ||
			args == "--version" {
			return "package_query"
		}
	}
	return ""
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

// zulipCronCommands are the management commands Zulip's own scheduler runs
// inside its container. Anything else from that path still alerts.
var zulipCronCommands = map[string]struct{}{
	"check_analytics_state":            {},
	"promote_new_full_members":         {},
	"send_zulip_update_announcements":  {},
	"update_analytics_counts":          {},
	"fetch_tor_exit_nodes":             {},
	"enqueue_digest_emails":            {},
	"clearsessions":                    {},
	"soft_deactivate_users":            {},
	"delete_old_unclaimed_attachments": {},
}

func zulipCronReason(args string) string {
	if !strings.Contains(args, "/home/zulip/deployments/current/") {
		return ""
	}
	fields := strings.Fields(args)
	for i, f := range fields {
		if f != "./manage.py" || i+1 >= len(fields) {
			continue
		}
		if _, ok := zulipCronCommands[fields[i+1]]; ok {
			return "app_cron"
		}
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
		parts = append(parts, "kprobe_args="+normalizeSocketArgs(string(body.Args)))
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
