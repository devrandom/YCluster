package main

import (
	"net"
	"net/url"
	"path"
	"strings"
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
	switch base {
	case "curl", "wget":
		if argumentsHaveLoopbackURL(args) {
			return "loopback_http"
		}
	case "incus":
		return incusReason(args, parentProc)
	case "sh", "bash", "dash", "zsh", "fish":
		return shellReason(args, parent)
	case "logrotate":
		if strings.Contains(args, "/etc/logrotate.conf") {
			return "cron"
		}
	case "snap":
		if strings.Contains(args, "microceph.log-rotate") {
			return "cron"
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
func shellReason(args, parent string) string {
	if reason := shellSystemReason(args); reason != "" {
		return reason
	}
	if shellAnsibleProbeReason(args) {
		return "ansible_probe"
	}
	if reason := shellPackageQueryReason(args); reason != "" {
		return reason
	}
	return shellLateReason(args, parent)
}

func shellSystemReason(args string) string {
	switch {
	case strings.Contains(args, "/usr/share/prometheus-node-exporter-collectors/"):
		return "node_exporter"
	case strings.Contains(args, "/etc/update-motd.d"):
		return "motd"
	case zulipCronReason(args) != "":
		return "app_cron"
	case strings.Contains(args, "debian-sa1"):
		// sysstat's daily-activity collector runs from cron every ten
		// minutes on every node with a fixed command line.
		return "sysstat"
	case strings.Contains(args, "/etc/cron."):
		// Debian's cron.d wrappers run through this exact shell command.
		return "cron"
	}
	return ""
}

// shellAnsibleProbeReason covers Ansible's become handshake and its
// connectivity/interpreter probes, which run on every task.
func shellAnsibleProbeReason(args string) bool {
	if strings.Contains(args, "BECOME-SUCCESS-") ||
		strings.Contains(args, "echo ~ && sleep 0") {
		return true
	}
	if strings.Contains(args, "echo PLATFORM") && strings.Contains(args, "command -v") {
		return true
	}
	return strings.Contains(args, "python3.") && strings.Contains(args, "&& sleep 0")
}

func shellPackageQueryReason(args string) string {
	switch {
	case strings.Contains(args, "type dpkg-query"), strings.Contains(args, "type rpm"):
		// needrestart probes the package tools from its dpkg/rpm hooks.
		return "package_query"
	case strings.Contains(args, "invoke-rc.d anacron start"):
		return "cron"
	}
	return ""
}

func shellLateReason(args, parent string) string {
	switch {
	case strings.TrimSpace(args) == "-c -- /bin/false":
		// Containers use a literal false command as a liveness placeholder.
		return "health_check"
	case strings.Contains(args, "/usr/lib/openssh/sftp-server"):
		// Ansible runs its file transfer through a shell wrapper around
		// sftp-server.
		return "ansible_probe"
	case strings.Contains(args, "/root/.ansible/tmp/"):
		// Ansible's per-task temp directory is created, chmodded and
		// removed with the same fixed command shapes.
		return "ansible_probe"
	case healthCheckArgs(args) && healthCheckLocal(args):
		return "health_check"
	case healthCheckArgs(args) && healthCheckParent(parent):
		// Container health checks run without a stable parent: the shim,
		// tini or the workload itself forks them, so match either the
		// probe shape or one of the known parent binaries.
		return "health_check"
	case parent == "/usr/sbin/sshd" && strings.Contains(args, "&& sleep 0"):
		return "ansible_probe"
	}
	return ""
}

func healthCheckLocal(args string) bool {
	return strings.Contains(args, "localhost") ||
		strings.Contains(args, "127.0.0.1") ||
		strings.Contains(args, "/lifecycle/")
}

func healthCheckParent(parent string) bool {
	switch parent {
	case "/lifecycle/ak", "/usr/bin/containerd-shim-runc-v2", "/usr/bin/runc", "/bin/sh", "/bin/bash":
		return true
	}
	return false
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
