package main

import (
	"net"
	"net/url"
	"path"
	"regexp"
	"strings"
)

var (
	ansibleBecomeRe = regexp.MustCompile(`^echo BECOME-SUCCESS-[A-Za-z0-9]+ ; /usr/bin/python3(?:\.\d+)?$`)
	ansibleSudoRe   = regexp.MustCompile(`^-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-[A-Za-z0-9]+ ; /usr/bin/python3(?:\.\d+)?"$`)
	ansibleSleepRe  = regexp.MustCompile(`^echo ~ && sleep 0$`)
	ansibleWrapRe   = regexp.MustCompile(`^/bin/sh -c ['"]echo ~ && sleep 0['"]$`)
	ansiblePlatRe   = regexp.MustCompile(`^echo PLATFORM(?:; (?:uname(?: -[a-z]+)*|echo (?:FOUND|ENDFOUND)|id(?: -u)?|command -v (?:'[A-Za-z0-9_./:+@-]+'|[A-Za-z0-9_./:+@-]+)))*$`)
	ansibleMkdirRe  = regexp.MustCompile(`^\( umask [0-9]+ && mkdir -p /root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+ \)$`)
	// Newer Ansible builds the temp directory through backquote echo
	// expansion. expandBackquoteEcho folds that back to the plain path, so
	// this matches the same fixed /root/.ansible/tmp template.
	ansibleExpandRe  = regexp.MustCompile(`^\( umask [0-9]+ && mkdir -p /root/\.ansible/tmp&& mkdir /root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+ && echo ansible-tmp-[A-Za-z0-9._-]+=/root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+ \)$`)
	ansibleCleanupRe = regexp.MustCompile(`^rm -f -r /root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+/ > /dev/null 2>&1$`)
	ansibleChmodRe   = regexp.MustCompile(`^chmod u\+x /root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+/ /root/\.ansible/tmp/ansible-tmp-[A-Za-z0-9._-]+/source$`)
	shellExecRe      = regexp.MustCompile(`^(?:/usr/bin/env -i \S+ )?(?:/bin|/usr/bin)/(?:sh|bash|dash) -c `)
	sudoExecRe       = regexp.MustCompile(`^sudo -H -S -n -u root /bin/sh -c `)
	backquoteEchoRe  = regexp.MustCompile("\"` echo ([^`]+) `\"")
	pythonProbeRe    = regexp.MustCompile(`^/usr/bin/python3(?:\.\d+)?$`)
	anacronBootRe    = regexp.MustCompile(`^\[ -x /etc/init\.d/anacron \] && if \[ ! -d /run/systemd/system \]; then /usr/sbin/invoke-rc\.d anacron start >/dev/null; fi$`)
	zulipManageRe    = regexp.MustCompile(`^cd /home/zulip/deployments/current/ && \./manage\.py (check_analytics_state|promote_new_full_members|send_zulip_update_announcements|update_analytics_counts|fetch_tor_exit_nodes|enqueue_digest_emails|clearsessions|soft_deactivate_users|delete_old_unclaimed_attachments)(?: >/dev/null)?$`)
	debianSA1Re      = regexp.MustCompile(`^(?:command -v debian-sa1 > ?/dev/null(?: 2>&1)? && )?debian-sa1 1 1$`)
	nodeExporterRe   = regexp.MustCompile(`^/usr/share/prometheus-node-exporter-collectors/[A-Za-z0-9_.-]+(?: \| sponge /var/lib/prometheus/node-exporter/[A-Za-z0-9_.-]+\.prom)?$`)
	snapRotateRe     = regexp.MustCompile(`^run --timer="?[0-9:,]+"? microceph\.log-rotate$`)
	snapDesktopRe    = regexp.MustCompile(`^run snapd-desktop-integration$`)
	certbotRenewRe   = regexp.MustCompile(`^(?:/usr/bin/certbot )?-q renew --no-random-sleep-on-renew$`)
	cupsLogrotateRe  = regexp.MustCompile(`(?s)^-c\s+"\s*invoke-rc\.d(?: --quiet)? cups restart > ?/dev/null\s*"(?:\s+logrotate_script\s+"/var/log/cups/[^"\r\n]+")?\s*$`)
	cronDirRe        = regexp.MustCompile(`^/etc/cron\.(hourly|daily|weekly|monthly|d)$`)
	packageQueryRe   = regexp.MustCompile(`^type (dpkg-query|rpm)(?: 1> /dev/null 2> /dev/null)?$`)
	aptStampRe       = regexp.MustCompile(`^touch /var/lib/apt/periodic/[A-Za-z0-9._-]+ 2>/dev/null \|\| true$`)
)

// routineExecReason classifies known cluster probes and automation. The
// binary allowlist is intentionally broad; without this filter, health
// checks and package introspection dominate process_exec alerts.
func routineExecReason(process tetragonProcess, parentProc *tetragonProc) string {
	base := path.Base(process.Binary)
	args := process.Arguments
	parent := ""
	if parentProc != nil {
		parent = parentProc.Binary
	}
	// Ansible's become handshake is routine only when a root-driven playbook
	// ran it. Any user who can sudo can type the same argv, and the escalation
	// class is the one place that must not be suppressible by a command line.
	invokerRoot := rootInvoker(parentProc)
	if process.Binary == "/etc/cron.daily/dpkg" {
		// Debian's daily package-database backup runs the script directly
		// from cron. The exact root-owned path is the identity; another
		// executable in a cron directory stays visible.
		return "cron"
	}
	switch base {
	case "curl", "wget":
		if process.Binary != "/usr/bin/curl" && process.Binary != "/usr/bin/wget" {
			break
		}
		if fetchVersionQuery(args) {
			return "package_query"
		}
		if publicFetchArgs(args) {
			return "public_egress"
		}
		if vipHealthProbe(process, parentProc) {
			return "loopback_http"
		}
	case "incus":
		if process.Binary == "/usr/bin/incus" || process.Binary == "/usr/local/bin/incus" {
			return incusReason(args, parentProc)
		}
	case "sh", "bash", "dash", "zsh", "fish":
		if !isSystemShell(process.Binary) {
			break
		}
		if publicFetchShell(args) {
			return "public_egress"
		}
		return shellReason(args, parent, invokerRoot)
	case "logrotate":
		if process.Binary != "/usr/sbin/logrotate" && process.Binary != "/usr/bin/logrotate" {
			break
		}
		if strings.Contains(args, "/etc/logrotate.conf") {
			return "cron"
		}
	case "snap":
		if process.Binary != "/usr/bin/snap" && !snapdSnapPath(process.Binary, "usr/bin/snap") {
			break
		}
		if snapRotateRe.MatchString(args) {
			return "cron"
		}
		if snapDesktopRe.MatchString(args) {
			return "desktop"
		}
	case "sudo":
		if process.Binary != "/usr/bin/sudo" {
			break
		}
		if invokerRoot && ansibleSudoRe.MatchString(args) {
			return "ansible_probe"
		}
	case "dpkg":
		if process.Binary != "/usr/bin/dpkg" {
			break
		}
		if strings.Contains(args, "--print-foreign-architectures") ||
			strings.Contains(args, "--compare-versions") ||
			args == "--version" || dpkgStatusArgs(args) {
			return "package_query"
		}
	}
	return ""
}

// dpkgStatusArgs matches the read-only package status query. Requiring
// exactly one operand keeps a payload appended after the package name from
// inheriting the exemption.
func dpkgStatusArgs(args string) bool {
	fields := strings.Fields(args)
	return len(fields) == 2 && fields[0] == "--status" && !strings.HasPrefix(fields[1], "-")
}

func fetchVersionQuery(args string) bool {
	fields := strings.Fields(args)
	return len(fields) == 1 && (fields[0] == "--version" || fields[0] == "-V")
}

// publicFetchArgs reports whether a curl/wget command line performs a plain
// HTTP(S) fetch and every URL it names is outside the cluster's private and
// loopback ranges. Config files, proxies, unix sockets and connect overrides
// stay visible because they can redirect the request somewhere the argv does
// not show. A private destination also stays visible: the public-egress
// exemption is only for public internet traffic.
func publicFetchArgs(args string) bool {
	fields := strings.Fields(args)
	for _, field := range fields {
		switch field {
		case "--unix-socket", "--abstract-unix-socket", "-K", "--config",
			"-x", "--proxy", "--preproxy", "--socks4", "--socks4a",
			"--socks5", "--socks5-hostname", "--resolve", "--connect-to",
			"-e", "--execute":
			return false
		}
		if strings.HasPrefix(field, "--config=") || strings.HasPrefix(field, "--proxy=") {
			return false
		}
	}
	seen := false
	for _, field := range fields {
		field = strings.Trim(field, "\"'")
		field = strings.TrimPrefix(field, "--url=")
		if !strings.HasPrefix(field, "http://") && !strings.HasPrefix(field, "https://") {
			continue
		}
		u, err := url.Parse(field)
		if err != nil || u.Hostname() == "" {
			return false
		}
		host := u.Hostname()
		if host == "localhost" || strings.HasSuffix(host, ".localhost") {
			return false
		}
		if ip := net.ParseIP(host); ip != nil &&
			(ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast()) {
			return false
		}
		seen = true
	}
	return seen
}

// publicFetchShell recognizes a shell whose entire payload is one plain
// public curl/wget fetch. Composition, pipes, substitutions and
// output redirection stay visible because they can turn the fetch into code
// execution or a private-network probe.
func publicFetchShell(args string) bool {
	cmd := shellPayload(args)
	if cmd == "" || strings.ContainsAny(cmd, ";|&<>`$\n\r") {
		return false
	}
	fields := strings.Fields(cmd)
	if len(fields) < 2 {
		return false
	}
	switch fields[0] {
	case "curl", "wget", "/usr/bin/curl", "/usr/bin/wget":
	default:
		return false
	}
	return publicFetchArgs(strings.Join(fields[1:], " "))
}

func isSystemShell(binary string) bool {
	switch binary {
	case "/bin/sh", "/usr/bin/sh",
		"/bin/bash", "/usr/bin/bash",
		"/bin/dash", "/usr/bin/dash",
		"/bin/zsh", "/usr/bin/zsh",
		"/bin/fish", "/usr/bin/fish":
		return true
	}
	return false
}

// vipHealthProbe matches keepalived's storage-VIP health check, the only
// curl the cluster runs on a host outside a container. The rule is anchored
// to the script keepalived execs (recorded as the curl's parent) and to the
// registry probe shape, so a bare curl or wget with a loopback URL is not
// routine by argv alone.
var vipHealthArgsRe = regexp.MustCompile(`^-f -s http://localhost:[0-9]+/v2/$`)

func vipHealthProbe(process tetragonProcess, parent *tetragonProc) bool {
	if process.Binary != "/usr/bin/curl" || parent == nil {
		return false
	}
	if parent.Binary != "/usr/local/lib/cluster/check-storage-vip-health" {
		return false
	}
	return vipHealthArgsRe.MatchString(process.Arguments)
}

// incusReason covers the periodic consumers of the Incus API. Each rule
// matches a known caller and command shape; anything else stays visible.
func incusReason(args string, parentProc *tetragonProc) string {
	// The VM reconciler and state sampler are known periodic API
	// consumers. Match the trusted caller once rather than every
	// subcommand shape a future reconcile step might add.
	if parentProc != nil && parentProc.Binary == "/usr/local/bin/ycluster" &&
		(strings.Contains(parentProc.Arguments, "ycluster vm reconcile") ||
			strings.Contains(parentProc.Arguments, "ycluster vm sample")) {
		return "vm_reconcile"
	}
	// vm-gpu-collector: on vfio-pci hosts the GPUs are invisible to the
	// host, so it lists VMs and execs nvidia-smi inside each one every 30s.
	if strings.Contains(args, "type=virtual-machine") && strings.Contains(args, "list") {
		return "gpu_collector"
	}
	if strings.Contains(args, "-- nvidia-smi --query-gpu=") && strings.Contains(args, "--format=csv") {
		return "gpu_collector"
	}
	if args == "list --format csv -c n,4" {
		// ids-bridge-consumer resolves a logged source address to a guest
		// name before it reports a bridge alert; the listing is read-only
		// inventory, cached for a minute.
		return "bridge_consumer"
	}
	// sync_transit_nics reads instance metadata and the bastion's
	// /etc/hosts on every reconciler tick to rewrite the transit block.
	if strings.HasPrefix(args, "file pull ") &&
		strings.HasSuffix(strings.TrimSpace(args), "/etc/hosts -") {
		return "nat_sync"
	}
	if strings.HasPrefix(args, "info ") {
		return "nat_sync"
	}
	return ""
}

// shellReason classifies the shell wrappers used by cron, Ansible, package
// tools and container health checks. The order matches the alert reasons:
// once a command line is recognised, later shapes are not considered.
func shellReason(args, parent string, invokerRoot bool) string {
	if cupsLogrotateRe.MatchString(args) {
		// Debian's cups logrotate postrotate script restarts the daemon
		// after rotating its logs. logrotate passes the script as a
		// multi-line -c payload and appends its own $0/$1 arguments.
		return "cron"
	}
	if reason := shellSystemReason(args); reason != "" {
		return reason
	}
	if invokerRoot && shellAnsibleProbeReason(args) {
		return "ansible_probe"
	}
	if reason := shellPackageQueryReason(args); reason != "" {
		return reason
	}
	return shellLateReason(args, parent)
}

func shellSystemReason(args string) string {
	cmd := shellPayload(args)
	switch {
	case nodeExporterRe.MatchString(cmd):
		return "node_exporter"
	case motdUpdate(cmd):
		return "motd"
	case cmd == "/usr/local/lib/cluster/quadlet-metrics-collector":
		// quadlet-metrics.timer execs the collector every 30 s. The unit
		// runs the shebang script directly, so the kernel records the
		// interpreter with the script as its only argument.
		return "quadlet_metrics"
	case zulipCronReason(cmd) != "":
		return "app_cron"
	case debianSA1Re.MatchString(cmd):
		// sysstat's daily-activity collector runs from cron every ten
		// minutes on every node with a fixed command line.
		return "sysstat"
	case cronRunParts(cmd):
		// Debian's cron.d wrappers run through this exact shell command.
		return "cron"
	}
	return ""
}

// shellAnsibleProbeReason covers Ansible's become handshake and its
// connectivity/interpreter probes, which run on every task.
func shellAnsibleProbeReason(args string) bool {
	return ansibleProbeCmd(shellPayload(args), 0)
}

// ansibleProbeCmd matches one of Ansible's probe payloads after unwrapping the
// shell layers that a non-root become path adds: the connection user's shell
// wraps the become shell, which wraps the interpreter handshake. Every layer
// must carry exactly one command, so a payload appended to a wrapper keeps the
// event visible.
func ansibleProbeCmd(cmd string, depth int) bool {
	raw := expandBackquoteEcho(normalizeShellQuotes(strings.TrimSpace(cmd)))
	cmd = trimAnsibleSleep(raw)
	if cmd == "" {
		return false
	}
	// The interpreter-discovery probe runs the interpreter and then sleeps;
	// the sleep is what marks it as a probe rather than a payload.
	if strings.HasSuffix(raw, "&& sleep 0") && pythonProbeRe.MatchString(cmd) {
		return true
	}
	if ansibleAnchoredProbe(cmd) {
		return true
	}
	if depth >= 4 {
		return false
	}
	inner, ok := unwrapAnsibleShell(cmd)
	if !ok {
		return false
	}
	return ansibleProbeCmd(inner, depth+1)
}

func ansibleAnchoredProbe(cmd string) bool {
	// trimAnsibleSleep strips the trailing `&& sleep 0` from the wrapper, so
	// the bare `echo ~` that precedes it is the same probe.
	return cmd == "echo ~" ||
		ansibleBecomeRe.MatchString(cmd) ||
		ansibleSudoRe.MatchString(cmd) ||
		ansibleSleepRe.MatchString(cmd) ||
		ansibleWrapRe.MatchString(cmd) ||
		ansibleMkdirRe.MatchString(cmd) ||
		ansibleExpandRe.MatchString(cmd) ||
		ansibleCleanupRe.MatchString(cmd) ||
		ansibleChmodRe.MatchString(cmd) ||
		(ansiblePlatRe.MatchString(cmd) && strings.Contains(cmd, "command -v"))
}

// normalizeShellQuotes folds the two escaped-quote forms a nested shell
// records into a plain quote, so the wrapper can be unwrapped and matched.
func normalizeShellQuotes(cmd string) string {
	cmd = strings.ReplaceAll(cmd, `'"'"'`, "'")
	return strings.ReplaceAll(cmd, `'\''`, "'")
}

func trimAnsibleSleep(cmd string) string {
	for {
		trimmed := strings.TrimSpace(strings.TrimSuffix(cmd, " && sleep 0"))
		if trimmed == cmd {
			return cmd
		}
		cmd = trimmed
	}
}

// expandBackquoteEcho folds Ansible's `"` echo <path> `"` expansion to the
// literal path it produces. It is only ever applied before an anchored match,
// so a crafted string still has to reproduce the whole template.
func expandBackquoteEcho(cmd string) string {
	return backquoteEchoRe.ReplaceAllString(cmd, "$1")
}

// unwrapAnsibleShell removes one `sh -c '...'` layer, optionally behind the
// become sudo shape.
func unwrapAnsibleShell(cmd string) (string, bool) {
	if m := sudoExecRe.FindString(cmd); m != "" {
		return strings.TrimSpace(trimOuterQuote(strings.TrimSpace(cmd[len(m):]))), true
	}
	if m := shellExecRe.FindString(cmd); m != "" {
		return strings.TrimSpace(trimOuterQuote(strings.TrimSpace(cmd[len(m):]))), true
	}
	return "", false
}

func shellPackageQueryReason(args string) string {
	cmd := shellPayload(args)
	switch {
	case packageQueryRe.MatchString(cmd):
		// needrestart probes the package tools from its dpkg/rpm hooks.
		return "package_query"
	case cmd == "invoke-rc.d anacron start" || anacronBootRe.MatchString(cmd):
		return "cron"
	}
	return ""
}

func shellLateReason(args, parent string) string {
	cmd := shellPayload(args)
	switch {
	case cmd == "/bin/false":
		// Containers use a literal false command as a liveness placeholder.
		return "health_check"
	case cmd == "/usr/lib/openssh/sftp-server":
		// Ansible runs its file transfer through a shell wrapper around
		// sftp-server.
		return "ansible_probe"
	case ansibleMkdirRe.MatchString(cmd):
		// Ansible's per-task temp directory is created, chmodded and
		// removed with the same fixed command shapes.
		return "ansible_probe"
	case healthCheckParent(parent) && (cmd == "/lifecycle/ak healthcheck" || safeHealthProbe(cmd)):
		// Container health checks run without a stable parent: the shim,
		// tini or the workload itself forks them, so require one of those
		// parents in addition to the probe shape. A loopback curl from an
		// arbitrary shell is an ordinary exec, not a health probe.
		return "health_check"
	}
	return ""
}

// shellPayload extracts the command passed to a shell's -c flag. Matching the
// command instead of the whole argument vector keeps a path in an unrelated
// later argument or a trailing comment from claiming a routine shape.
func shellPayload(args string) string {
	// A newline separates commands in a shell. strings.Fields would fold it
	// into a space and let the second command read as part of the first, so
	// refuse to classify a multi-line payload at all.
	if strings.ContainsAny(args, "\n\r") {
		return ""
	}
	fields := strings.Fields(args)
	for i, field := range fields {
		if field != "-c" || i+1 >= len(fields) {
			continue
		}
		cmd := strings.Join(fields[i+1:], " ")
		cmd = strings.TrimSpace(cmd)
		cmd = strings.TrimPrefix(cmd, "-- ")
		return strings.TrimSpace(trimOuterQuote(cmd))
	}
	return trimOuterQuote(strings.TrimSpace(args))
}

func trimOuterQuote(s string) string {
	if len(s) >= 2 && (s[0] == '"' || s[0] == '\'') && s[len(s)-1] == s[0] {
		return s[1 : len(s)-1]
	}
	return s
}

// motdUpdate reports whether the command runs /etc/update-motd.d through
// run-parts, optionally through `env -i` and writing the generated file. The
// directory must be the final operand once the redirect is removed, so a
// payload appended to the wrapper does not inherit the exemption.
func motdUpdate(cmd string) bool {
	fields := strings.Fields(cmd)
	if len(fields) >= 3 && fields[0] == "/usr/bin/env" && fields[1] == "-i" {
		fields = fields[2:]
	}
	if n := len(fields); n >= 3 && fields[n-2] == ">" && fields[n-1] == "/run/motd.dynamic.new" {
		fields = fields[:n-2]
	}
	if len(fields) == 0 || fields[len(fields)-1] != "/etc/update-motd.d" {
		return false
	}
	for _, field := range fields {
		if field == "run-parts" {
			return true
		}
	}
	return false
}

func cronRunParts(cmd string) bool {
	fields := strings.Fields(cmd)
	if len(fields) == 0 || !cronDirRe.MatchString(fields[len(fields)-1]) {
		return false
	}
	if fields[0] == "run-parts" {
		return true
	}
	return len(fields) > 3 && fields[0] == "cd" && fields[1] == "/" && fields[2] == "&&" && fields[3] == "run-parts"
}

func loopbackHTTPProbe(cmd string) bool {
	if strings.ContainsAny(cmd, ";|&<>`$\n\r") || hasHTTPRedirectFlag(cmd) {
		// A localhost URL over the Incus/Docker socket, through a proxy, or
		// redirected by --resolve/--connect-to is not a health probe.
		return false
	}
	fields := strings.Fields(cmd)
	if len(fields) == 0 {
		return false
	}
	switch fields[0] {
	case "curl", "wget", "/usr/bin/curl", "/usr/bin/wget":
	default:
		return false
	}
	return argumentsHaveLoopbackURL(cmd)
}

func safeHealthProbe(cmd string) bool {
	fields := strings.Fields(cmd)
	if len(fields) == 0 {
		return false
	}
	if strings.ContainsAny(cmd, ";|&<>`$\n\r") ||
		hasHTTPRedirectFlag(cmd) {
		return false
	}
	if fields[0] == "false" || fields[0] == "/bin/false" || fields[0] == "/usr/bin/false" {
		return true
	}
	lower := strings.ToLower(cmd)
	switch fields[0] {
	case "curl", "wget", "/usr/bin/curl", "/usr/bin/wget":
		return loopbackHTTPProbe(cmd)
	case "redis-cli", "/usr/bin/redis-cli":
		return strings.HasSuffix(lower, " ping") &&
			!hasShortFlag(cmd, "-h") && !strings.Contains(cmd, "--host") &&
			!hasShortFlag(cmd, "-s") && !strings.Contains(cmd, "--socket")
	case "pg_isready", "/usr/bin/pg_isready":
		return !hasShortFlag(cmd, "-h") && !strings.Contains(cmd, "--host")
	case "mc", "/usr/local/bin/mc":
		return strings.Contains(lower, " ready") &&
			!strings.Contains(cmd, " --host") && !strings.Contains(cmd, " -H ") &&
			!strings.Contains(cmd, "--config-dir")
	case "grpc_health_probe", "/usr/local/bin/grpc_health_probe":
		for i, field := range fields {
			value := ""
			switch {
			case field == "-addr" && i+1 < len(fields):
				value = fields[i+1]
			case strings.HasPrefix(field, "-addr="):
				value = strings.TrimPrefix(field, "-addr=")
			default:
				continue
			}
			host := value
			if strings.HasPrefix(host, "[") {
				if end := strings.Index(host, "]"); end > 0 {
					host = host[1:end]
				}
			} else if h, _, ok := strings.Cut(host, ":"); ok {
				host = h
			}
			return host == "127.0.0.1" || host == "localhost" || host == "::1"
		}
		return true
	default:
		return false
	}
}

func healthCheckParent(parent string) bool {
	switch parent {
	case "/lifecycle/ak", "/usr/bin/containerd-shim-runc-v2", "/usr/bin/runc", "/usr/bin/tini":
		return true
	}
	return false
}

func zulipCronReason(args string) string {
	if zulipManageRe.MatchString(args) {
		return "app_cron"
	}
	return ""
}

func argumentsHaveLoopbackURL(args string) bool {
	if strings.ContainsAny(args, ";|&<>`$\n\r") || hasHTTPRedirectFlag(args) {
		return false
	}
	seen := false
	for _, field := range strings.Fields(args) {
		field = strings.Trim(field, "\"'")
		u, err := url.Parse(field)
		if err != nil || u.Scheme == "" || u.Host == "" {
			continue
		}
		if u.Scheme != "http" && u.Scheme != "https" {
			continue
		}
		seen = true
		host := u.Hostname()
		if host == "localhost" || strings.HasSuffix(host, ".localhost") {
			continue
		}
		if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
			continue
		}
		return false
	}
	return seen
}

func hasHTTPRedirectFlag(args string) bool {
	fields := strings.Fields(args)
	for i, field := range fields {
		switch field {
		case "-o", "--output", "--output-document":
			// curl -o /dev/null is the ubiquitous probe discard.
			if i+1 < len(fields) && fields[i+1] == "/dev/null" {
				continue
			}
			return true
		case "--unix-socket", "--abstract-unix-socket", "--proxy", "-x",
			"--resolve", "--connect-to", "--preproxy", "--socks4",
			"--socks5", "--socks5-hostname", "-K", "--config", "-O",
			"-e", "--execute",
			"-i", "--input-file", "-B", "--base", "-P", "--directory-prefix",
			"-a", "--append-output", "-T", "--upload-file", "-F", "--form",
			"-D", "--dump-header", "-b", "--cookie", "-c", "--cookie-jar":
			return true
		}
		if strings.HasPrefix(field, "--proxy=") ||
			strings.HasPrefix(field, "--resolve=") ||
			strings.HasPrefix(field, "--connect-to=") ||
			strings.HasPrefix(field, "--config=") ||
			strings.HasPrefix(field, "--execute=") ||
			strings.HasPrefix(field, "--unix-socket=") ||
			strings.HasPrefix(field, "--abstract-unix-socket=") ||
			strings.HasPrefix(field, "--socks5=") ||
			strings.HasPrefix(field, "--input-file=") ||
			strings.HasPrefix(field, "--base=") ||
			strings.HasPrefix(field, "--directory-prefix=") ||
			strings.HasPrefix(field, "--append-output=") ||
			strings.HasPrefix(field, "--upload-file=") ||
			strings.HasPrefix(field, "--form=") ||
			strings.HasPrefix(field, "--dump-header=") ||
			strings.HasPrefix(field, "--cookie=") ||
			strings.HasPrefix(field, "--cookie-jar=") ||
			(strings.HasPrefix(field, "-K") && field != "-K") ||
			(strings.HasPrefix(field, "-e") && field != "-e") ||
			(strings.HasPrefix(field, "-x") && field != "-x") {
			return true
		}
		if unsafeShortCluster(field) {
			return true
		}
	}
	return hasUnsafeOutput(fields)
}

// shortValueOpts lists the short options of curl and wget that take a value.
// The union covers both tools, so a clustered flag ends at the first letter
// that can consume the rest of the token as its value.
var shortValueOpts = map[rune]bool{
	'o': true, 'O': true, 'K': true, 'x': true, 'e': true, 'b': true,
	'c': true, 'T': true, 'F': true, 'D': true, 'E': true, 'y': true,
	'Y': true, 'z': true, 'U': true, 'P': true, 'Q': true, 't': true,
	'w': true, 'H': true, 'd': true, 'A': true, 'u': true, 'm': true,
	'r': true, 'C': true, 'X': true, 'i': true, 'B': true, 'a': true,
}

// shortUnsafeOpts are the value-taking letters that read a config or a local
// file, write one, or redirect the request. A health probe never needs them,
// so any cluster that contains one is not routine.
var shortUnsafeOpts = map[rune]bool{
	'o': true, 'O': true, 'K': true, 'x': true, 'e': true, 'b': true,
	'c': true, 'T': true, 'F': true, 'D': true, 'E': true, 'y': true,
	'Y': true, 'i': true, 'B': true, 'a': true, 'P': true,
}

// unsafeShortCluster reports whether a single-dash option cluster such as
// `-so` or `-sK` hides a file, config or redirect option. Runes after the
// first value-taking option are that option's value and are not options.
func unsafeShortCluster(field string) bool {
	if !strings.HasPrefix(field, "-") || strings.HasPrefix(field, "--") || field == "-" {
		return false
	}
	if field == "-o/dev/null" {
		return false
	}
	for _, r := range strings.TrimPrefix(field, "-") {
		if shortUnsafeOpts[r] {
			if r == 'o' && field == "-o" {
				return false // the separated /dev/null form is checked above
			}
			return true
		}
		if shortValueOpts[r] {
			return false
		}
	}
	return false
}

// hasUnsafeOutput allows the ubiquitous `curl -o /dev/null` shape and
// rejects output to any other path, including attached forms.
func hasUnsafeOutput(fields []string) bool {
	for i, field := range fields {
		switch field {
		case "-o", "--output", "--output-document":
			if i+1 < len(fields) && fields[i+1] == "/dev/null" {
				continue
			}
			return true
		case "-O":
			return true
		}
		if strings.HasPrefix(field, "--output=") || strings.HasPrefix(field, "--output-document=") {
			if strings.HasSuffix(field, "=/dev/null") {
				continue
			}
			return true
		}
		if strings.HasPrefix(field, "-o") && field != "-o" && field != "-o/dev/null" {
			return true
		}
	}
	return false
}

// hasShortFlag matches both separated and attached short options.
func hasShortFlag(args, flag string) bool {
	for _, field := range strings.Fields(args) {
		if field == flag || (strings.HasPrefix(field, flag) && !strings.HasPrefix(field, "--")) {
			return true
		}
	}
	return false
}
