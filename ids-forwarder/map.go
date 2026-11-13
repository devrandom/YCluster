package main

import (
	"encoding/json"
	"fmt"
	"path"
	"strconv"
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
	// A process that started before the sensor attached has no exec record;
	// fill in the identity from /proc so routing, suppression and labels name
	// the binary that is actually running.
	resolveEventIdentity(&body.Process)
	if !configuredEvent(cfg.events, evType) {
		return alert{}, meta, false, ""
	}
	// Root executing a payload staged in a user-writable tree crosses the
	// privilege boundary without needing a package-manager shell. Containers
	// are excluded: a root process inside a tenant container sees its own
	// filesystem, not the host's.
	if evType == "process_exec" && cfg.execRootUserPath && body.Process.UID == 0 {
		if layer, _, source := tetragonEventLayerInfo(body); layer == "host" && rootExecUserPath(body.Process.Binary) {
			a := newAlert(cfg, sourceTetragon, "warning", layer, "exec-root-user-path",
				"process_exec: binary="+body.Process.Binary+" root-exec")
			a.Labels["binary"] = processBinaryName(body.Process.Binary)
			applyOrigin(&a, body, layer)
			applyIdentity(&a, evType, body, layer)
			a.prov.layer = source
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
	layer, containerName, layerSource := tetragonEventLayerInfo(body)
	prov := eventProvenance{layer: layerSource}
	if reason := tetragonSuppression(evType, body, layer); reason != "" {
		// Suppressed events still made a layer decision. Hand it back so the
		// caller can count it: the rollout question is whether the old /proc
		// path still decides anything, and most decisions are on events that
		// never become alerts.
		return alert{prov: prov}, meta, false, reason
	}
	summary := eventSummary(evType, body)
	a := newAlert(cfg, sourceTetragon, tetragonSeverity(body), layer, tetragonRule(evType, body), summary)
	a.prov = prov
	applyOrigin(&a, body, layer)
	applyIdentity(&a, evType, body, layer)
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
	layer, containerName, _ = tetragonEventLayerInfo(body)
	return layer, containerName
}

// tetragonEventLayerInfo is tetragonEventLayer plus the provenance source the
// metric counts: metadata, namespace, cgroup, uid or fallback.
func tetragonEventLayerInfo(body *tetragonLog) (layer, containerName, source string) {
	layer = tetragonLayer(body)
	if layer != "host" {
		return layer, "", "metadata"
	}
	// The host-side exec rules come after the layer check so a container's
	// shells are attributed to the container rather than matched against
	// host daemon shapes.
	r := resolveEventLayer(body)
	if r.Layer != "" {
		return r.Layer, r.Name, r.Source
	}
	return "host", "", r.Source
}

// applyOrigin records which login session or systemd unit started the
// process. Provenance only: nothing suppresses, coalesces or routes on it
// yet, it exists so the stream can be measured per actor first. Every alert
// carries an origin, including "unknown", so a resolution regression shows
// up as a series rather than disappearing.
func applyOrigin(a *alert, body *tetragonLog, layer string) {
	if layer == "container" {
		a.Labels["origin"] = "container"
		return
	}
	if layer == "unverified" {
		a.Labels["origin"] = "unverified"
		return
	}
	o := eventOrigin(body.Process, body.Parent)
	a.Labels["origin"] = o.Origin
	if o.Actor == "" {
		return
	}
	// These labels are set after newAlert's redaction pass, so they get it
	// here instead of trusting the value to be address-free.
	a.Labels["actor"] = redactIPs(o.Actor)
}

// applyIdentity records what the delivery classifier reads: the host-visible
// uid, the event kind, and whether the event is the transition out of a
// non-root login session.
func applyIdentity(a *alert, evType string, body *tetragonLog, layer string) {
	// A zero-value process is a missing process, not uid 0: the decoder
	// leaves every field at its zero value. Only a real event carries a pid,
	// so the absence of one must not read as proof of host root.
	if body.Process.PID != 0 {
		a.Labels["uid"] = strconv.FormatUint(uint64(body.Process.UID), 10)
	}
	a.Labels["kind"] = eventKind(evType, body)
	// Only the exec is the transition marker. The credential commit that
	// follows from the same process is the same action, and paging twice for
	// one sudo would double the only class that pages at landing.
	// Only a container is excluded: a tenant root is not host root. An
	// unverified layer means the process and its parent had left /proc, not
	// that the process is a tenant, so the escalation still counts there.
	if a.Labels["kind"] != "exec" || layer == "container" {
		return
	}
	// An unverified layer is two different things. If the cgroup positively
	// named a user-scope runtime or a user-created sandbox, the process is a
	// tenant and never crossed a host boundary. If the layer is unverified
	// only because the uid fallback ran, nothing was decided and the event
	// stays coverage.
	if layer == "unverified" && tenantUnverified(a.prov.layer) {
		return
	}
	if !privilegeTools[body.Process.Binary] {
		return
	}
	if source := escalationSource(body); source != "" {
		a.Labels["escalation"] = "nonroot"
		a.prov.escalation = source
	}
}

// eventKind names the event independently of the policy that produced it, so
// the classifier does not depend on policy file names. The exec policy also
// reports file writes, which is why the event type alone is not enough.
func eventKind(evType string, body *tetragonLog) string {
	switch evType {
	case "process_exec":
		return "exec"
	}
	if body.FunctionName == "tcp_connect" {
		return "connect"
	}
	switch body.PolicyName {
	case "kernel-modules":
		return "module"
	case "privilege-raises":
		return "privilege"
	case "exec-and-sensitive-file-writes":
		return "write"
	}
	return "other"
}

// tetragonSuppression returns the reason an event is not forwarded, or ""
// when it should produce an alert.
func tetragonSuppression(evType string, body *tetragonLog, layer string) string {
	if evType == "process_exec" && layer == "container" {
		return "container_internal"
	}
	if layer == "container" {
		return containerSuppressionReason(body)
	}
	if layer == "unverified" {
		return containerEgressReason(body, "unverified")
	}
	if evType == "process_exec" {
		if reason := routineExecReason(body.Process, body.Parent); reason != "" {
			return reason
		}
	}
	if reason := packageMaintenanceReason(body); reason != "" {
		return reason
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
	if reason := routineModuleReason(body); reason != "" {
		return reason
	}
	if reason := routinePrivilegeReason(body); reason != "" {
		return reason
	}
	return routineFileWriteReason(body)
}

// routineModuleReason covers the modules the VM bridge guard's own nftables
// ruleset pulls in when it is loaded: the conntrack expression and the log
// statement. Every guard (re)load otherwise reports two module loads per
// host, which is our noise rather than a signal.
func routineModuleReason(body *tetragonLog) string {
	if body == nil || body.PolicyName != "kernel-modules" {
		return ""
	}
	binary := strings.TrimSuffix(body.Process.Binary, " (deleted)")
	if binary != "/usr/sbin/modprobe" && binary != "/sbin/modprobe" {
		return ""
	}
	var modules []string
	for _, field := range strings.Fields(body.Process.Arguments) {
		if !strings.HasPrefix(field, "-") {
			modules = append(modules, field)
		}
	}
	if len(modules) != 1 {
		return ""
	}
	switch modules[0] {
	case "nf_conntrack_bridge", "nft-expr-log":
		return "bridge_guard_module"
	}
	return ""
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
		if rootInvoker(body.Parent) && ansibleSudoRe.MatchString(body.Process.Arguments) {
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
	case "/usr/bin/unattended-upgrade":
		// apt's unattended run drops to _apt for downloads and back again
		// on every timer tick.
		return "package_maintenance"
	case "/usr/bin/certbot":
		// certbot renew drops privileges after loading its account key on
		// every timer tick. Other certbot subcommands stay visible.
		if certbotRenewRe.MatchString(body.Process.Arguments) {
			return "expected_privilege"
		}
	case "/usr/sbin/squid", "/usr/bin/squid":
		// `squid -k rotate` re-execs itself as the cache user to rotate
		// its logs; logrotate drives it.
		if strings.Contains(body.Process.Arguments, "-k rotate") {
			return "expected_privilege"
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

// packageMaintenanceReason classifies apt's own maintenance shapes, and
// only those: installs, removals and dpkg file installs stay visible.
func packageMaintenanceReason(body *tetragonLog) string {
	if body == nil {
		return ""
	}
	binary := strings.TrimSuffix(body.Process.Binary, " (deleted)")
	args := body.Process.Arguments
	switch binary {
	case "/usr/bin/apt-get", "/usr/bin/apt":
		// Only the subcommand position is routine: a package named "update"
		// in `apt-get install evil update` is still an install.
		fields := strings.Fields(args)
		for _, field := range fields {
			if strings.Contains(field, "Pre-Invoke") || strings.Contains(field, "Post-Invoke") ||
				field == "-c" || field == "--config-file" ||
				strings.HasPrefix(field, "--config-file=") ||
				(strings.HasPrefix(field, "-c") && field != "-c") {
				return ""
			}
		}
		sub, rest := aptSubcommand(fields)
		switch sub {
		case "update", "check":
			return "package_update"
		case "list":
			if len(rest) > 0 && strings.Contains(rest[0], "upgradable") {
				return "package_update"
			}
		}
	case "/usr/bin/dpkg":
		if strings.HasPrefix(strings.TrimSpace(args), "-l ") {
			return "package_update"
		}
	case "/bin/sh", "/bin/bash", "/usr/bin/bash", "/usr/bin/dash":
		cmd := shellPayload(args)
		if cmd == "systemctl start --no-block apt-news.service esm-cache.service" ||
			aptStampRe.MatchString(cmd) {
			return "package_update"
		}
	}
	script := "/usr/lib/ubuntu-advantage/esm_cache.py"
	if binary == script || invokesScript(args, script) || invokesScript(shellPayload(args), script) {
		return "package_update"
	}
	return ""
}

// aptSubcommand returns the first non-option operand and the fields after it,
// skipping the values of apt options that take a separate argument. This keeps
// a package name that happens to be "update" from looking like the subcommand.
func aptSubcommand(fields []string) (string, []string) {
	for i := 0; i < len(fields); i++ {
		field := fields[i]
		if strings.HasPrefix(field, "-") {
			switch field {
			case "-o", "--option", "-c", "--config-file", "-t", "--target-release":
				i++
			}
			continue
		}
		return field, fields[i+1:]
	}
	return "", nil
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
	return body.Process.Binary == "/usr/bin/incus" || body.Process.Binary == "/usr/local/bin/incus"
}

func isIncusForkfile(body *tetragonLog) bool {
	if body == nil || body.Process.Binary == "" {
		return false
	}
	return (body.Process.Binary == "/usr/bin/incusd" ||
		body.Process.Binary == "/usr/libexec/incus/incusd" ||
		body.Process.Binary == "/usr/local/bin/incusd") &&
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
	// Docker containers carry the runtime's container id on the event, which
	// is the only attribution that survives a short-lived child: a health
	// check is gone from /proc before the line is read, and its uid is a
	// host uid, so the cgroup fallback cannot place it.
	if body.Process.Docker != "" {
		return "container"
	}
	if body.Parent != nil && body.Parent.Docker != "" {
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
