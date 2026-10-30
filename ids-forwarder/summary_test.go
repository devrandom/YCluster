package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRoutineExecSuppression(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	cases := []struct {
		name   string
		binary string
		args   string
		parent string
		reason string
	}{
		{
			name:   "loopback health request",
			binary: "/usr/bin/curl",
			args:   "-f -s http://localhost:5000/v2/",
			parent: "/usr/local/lib/cluster/check-storage-vip-health",
			reason: "loopback_http",
		},
		{
			name:   "container health shell",
			binary: "/bin/sh",
			args:   `-c "curl --silent --fail http://localhost:8080/health"`,
			parent: "/usr/bin/containerd-shim-runc-v2",
			reason: "health_check",
		},
		{
			name:   "application healthcheck",
			binary: "/usr/bin/bash",
			args:   "/lifecycle/ak healthcheck",
			parent: "/lifecycle/ak",
			reason: "health_check",
		},
		{
			name:   "node exporter collector",
			binary: "/bin/sh",
			args:   `-c "/usr/share/prometheus-node-exporter-collectors/smartmon.sh | sponge out"`,
			parent: "/proc/self/fd/9",
			reason: "node_exporter",
		},
		{
			name:   "node exporter without parent attribution",
			binary: "/bin/sh",
			args:   `-c "/usr/share/prometheus-node-exporter-collectors/nvme_metrics.py | sponge out"`,
			parent: "",
			reason: "node_exporter",
		},
		{
			name:   "sysstat collector",
			binary: "/bin/sh",
			args:   `-c "command -v debian-sa1 > /dev/null && debian-sa1 1 1"`,
			parent: "/usr/sbin/cron",
			reason: "sysstat",
		},
		{
			name:   "ansible platform probe",
			binary: "/bin/sh",
			args:   `-c "echo PLATFORM; uname; echo FOUND; command -v 'python3.12'; command -v 'python3.11'"`,
			parent: "/bin/bash",
			reason: "ansible_probe",
		},
		{
			name:   "ansible connectivity wrapper",
			binary: "/bin/bash",
			args:   `-c "/bin/sh -c 'echo ~ && sleep 0'"`,
			parent: "/usr/sbin/sshd",
			reason: "ansible_probe",
		},
		{
			name:   "ansible become handshake",
			binary: "/bin/sh",
			args:   `-c "echo BECOME-SUCCESS-abc123 ; /usr/bin/python3"`,
			parent: "/usr/bin/bash",
			reason: "ansible_probe",
		},
		{
			name:   "storage mount probe",
			binary: "/bin/sh",
			args:   `-c "mountpoint -q /rbd/user"`,
			parent: "/usr/bin/bash",
			reason: "storage_probe",
		},
		{
			name:   "login motd",
			binary: "/bin/sh",
			args:   `-c -- "/usr/bin/env -i PATH=/usr/bin run-parts --lsbsysinit /etc/update-motd.d"`,
			parent: "",
			reason: "motd",
		},
		{
			name:   "ansible sudo probe",
			binary: "/usr/bin/sudo",
			args:   `-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-abc123 ; /usr/bin/python3"`,
			parent: "/bin/sh",
			reason: "ansible_probe",
		},
		{
			name:   "ansible ssh probe",
			binary: "/usr/bin/bash",
			args:   `-c "echo ~ && sleep 0"`,
			parent: "/usr/sbin/sshd",
			reason: "ansible_probe",
		},
		{
			name:   "vm gpu collector list",
			binary: "/usr/bin/incus",
			args:   "list --format=json type=virtual-machine",
			parent: "/usr/bin/python3",
			reason: "gpu_collector",
		},
		{
			name:   "vm gpu collector query",
			binary: "/usr/bin/incus",
			args:   "exec vm1 -- nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader,nounits",
			parent: "/usr/bin/python3",
			reason: "gpu_collector",
		},
		{
			name:   "frontend nat hosts read",
			binary: "/usr/bin/incus",
			args:   "file pull gw1/etc/hosts -",
			parent: "/usr/bin/python3",
			reason: "nat_sync",
		},
		{
			name:   "frontend nat instance info",
			binary: "/usr/bin/incus",
			args:   "info gw1",
			parent: "/usr/bin/python3",
			reason: "nat_sync",
		},
		{
			name:   "nas mount retry probe",
			binary: "/bin/sh",
			args:   `-c '! mountpoint -q /mnt/nas-vm'`,
			parent: "/usr/lib/systemd/systemd",
			reason: "mount_probe",
		},
		{
			name:   "nas mount retry start",
			binary: "/bin/sh",
			args:   `-c 'systemctl start "mnt-nas\x2dvm.mount"'`,
			parent: "/usr/lib/systemd/systemd",
			reason: "mount_retry",
		},
		{
			name:   "package query",
			binary: "/usr/bin/dpkg",
			args:   "--print-foreign-architectures",
			parent: "/usr/bin/apt-config",
			reason: "package_query",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := execLine(t, tc.binary, tc.args, tc.parent)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("routine exec must not forward")
			}
			if reason != tc.reason {
				t.Fatalf("reason = %q, want %q", reason, tc.reason)
			}
		})
	}
}

func TestRoutineExecKeepsUsefulCommands(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	cases := []struct {
		binary string
		args   string
		parent string
	}{
		{"/usr/bin/curl", "https://example.com/health", "/usr/bin/bash"},
		{"/usr/bin/bash", `-c "id"`, "/usr/sbin/sshd"},
		{"/usr/bin/apt-get", "install nginx", "/usr/bin/sudo"},
		{"/bin/sh", `-c "git-upload-pack '/srv/git/project.git'"`, "/usr/bin/runc"},
		{"/bin/sh", `-c "mountpoint -q /mnt/data"`, "/usr/bin/bash"},
		{"/usr/bin/incus", "file push gw1/etc/hosts /tmp/hosts", "/usr/bin/python3"},
		{"/usr/bin/incus", "exec gw1 -- id", "/usr/bin/python3"},
	}

	for _, tc := range cases {
		line := execLine(t, tc.binary, tc.args, tc.parent)
		if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
			t.Fatalf("%s %q was suppressed as %q", tc.binary, tc.args, reason)
		}
	}
}

func TestSystemdPrivilegeFallback(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	for _, binary := range []string{
		"/proc/self/fd/9",
		"/usr/lib/systemd/systemd",
		"/lib/systemd/systemd",
	} {
		t.Run(binary, func(t *testing.T) {
			line := privilegeLine(t, "privilege-raises", "__sys_setresuid", binary)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("expected systemd transient privilege transition to be suppressed")
			}
			if reason != "expected_privilege" {
				t.Fatalf("reason = %q, want expected_privilege", reason)
			}
		})
	}

	for _, binary := range []string{"/usr/sbin/sshd", "/usr/sbin/cron"} {
		t.Run(binary+" policy-owned", func(t *testing.T) {
			line := privilegeLine(t, "privilege-raises", "__sys_setresuid", binary)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("policy-owned privilege event was suppressed as %q", reason)
			}
		})
	}
}

func privilegeLineUID(t *testing.T, policy, function, binary, args string, uid uint32) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   policy,
			"function_name": function,
			"process": map[string]any{
				"binary":    binary,
				"arguments": args,
				"uid":       uid,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func privilegeLine(t *testing.T, policy, function, binary string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   policy,
			"function_name": function,
			"process":       map[string]any{"binary": binary},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestDedupSummaryNormalizesVolatileValues(t *testing.T) {
	cfg := testConfig()
	first := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec",
		`process_exec: binary=/usr/bin/sudo args=-c "echo BECOME-SUCCESS-abc123 ; python3" file=/etc/prometheus/tmpabc_123`)
	second := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec",
		`process_exec: binary=/usr/bin/sudo args=-c "echo BECOME-SUCCESS-xyz789 ; python3" file=/etc/prometheus/tmpdef_456`)

	if first.dedupKey() != second.dedupKey() {
		t.Fatalf("volatile identifiers must not defeat dedup: %s != %s", first.dedupKey(), second.dedupKey())
	}
	if first.Annotations["summary"] == second.Annotations["summary"] {
		t.Fatal("visible summaries should retain their original identifiers")
	}

	other := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec",
		`process_exec: binary=/usr/bin/bash args=-c "id"`)
	if first.dedupKey() == other.dedupKey() {
		t.Fatal("distinct stable commands must not collapse")
	}
}

func TestTCPSummaryDropsPerConnectionFields(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	line := func(sport, cookie string) string {
		return `{"process_kprobe":{"function_name":"tcp_connect","process":{"binary":"/opt/app/worker","arguments":"--serve"},"args":[{"sock_arg":{"family":"AF_INET","saddr":"10.0.0.12","daddr":"172.21.0.2","sport":` +
			sport + `,"dport":8080,"cookie":"` + cookie + `","state":"TCP_SYN_SENT"}}]},"time":"2024-01-01T00:00:00Z"}`
	}
	first, _, ok := tetragonAlertWithMeta(line("35052", "18446617195950414784"), cfg)
	if !ok {
		t.Fatal("tcp event should be forwarded")
	}
	second, _, ok := tetragonAlertWithMeta(line("41234", "18446617199922085888"), cfg)
	if !ok {
		t.Fatal("tcp event should be forwarded")
	}

	if strings.Contains(first.Annotations["summary"], "sport") ||
		strings.Contains(first.Annotations["summary"], "cookie") {
		t.Fatalf("summary keeps per-connection fields: %s", first.Annotations["summary"])
	}
	if first.dedupKey() != second.dedupKey() {
		t.Fatalf("per-connection fields defeat dedup: %s != %s", first.dedupKey(), second.dedupKey())
	}
	if !strings.Contains(first.Annotations["summary"], "dport") {
		t.Fatalf("summary should keep the destination: %s", first.Annotations["summary"])
	}
}

func TestAlertSummaryIsBounded(t *testing.T) {
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "test", strings.Repeat("x", maxSummaryBytes+100))
	got := a.Annotations["summary"]
	if len(got) > maxSummaryBytes {
		t.Fatalf("summary length = %d, want <= %d", len(got), maxSummaryBytes)
	}
	if !strings.HasSuffix(got, "...") {
		t.Fatalf("bounded summary should end with ellipsis: %q", got[len(got)-10:])
	}
}

func TestMetricsSuppressedCounts(t *testing.T) {
	var m metrics
	m.suppress("")
	m.suppress("health_check")
	m.suppress("health_check")
	m.suppress("loopback_http")

	got := m.suppressedCounts()
	if got["health_check"] != 2 || got["loopback_http"] != 1 || len(got) != 2 {
		t.Fatalf("unexpected suppressed counts: %#v", got)
	}
}

func TestHandleLineCountsSuppressedExec(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	line := execLine(t, "/usr/bin/curl", "-f -s http://localhost:5000/v2/", "/usr/bin/bash")
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	if got := f.stats.suppressedCounts()["loopback_http"]; got != 1 {
		t.Fatalf("loopback_http suppressed count = %d, want 1", got)
	}
}

func execLine(t *testing.T, binary, args, parent string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"binary":    binary,
				"arguments": args,
				"parent":    map[string]any{"binary": parent},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestRuntimePrivilegeTransitionsAreSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	for _, binary := range []string{
		"/usr/bin/runc",
		"/usr/bin/containerd-shim-runc-v2",
		"/usr/bin/docker-proxy",
		"/usr/libexec/incus/incusd",
		"/usr/libexec/virtiofsd",
		"/usr/libexec/podman/rootlessport",
		"/usr/lib/pcp/pmdas/proc/pmdaproc",
	} {
		for _, function := range []string{"__sys_setresuid", "security_capset"} {
			t.Run(binary+" "+function, func(t *testing.T) {
				line := privilegeLine(t, "privilege-raises", function, binary)
				_, _, ok, reason := tetragonAlertDecision(line, cfg)
				if ok {
					t.Fatal("expected container runtime privilege transition to be suppressed")
				}
				if reason != "expected_privilege" {
					t.Fatalf("reason = %q, want expected_privilege", reason)
				}
			})
		}
	}

	// A shell raising to root is still the signal we want.
	line := privilegeLine(t, "privilege-raises", "__sys_setresuid", "/bin/sh")
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
		t.Fatalf("shell privilege raise was suppressed as %q", reason)
	}
}

func TestSudoDropFromRootIsNotARaise(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	drops := []string{
		"-u qdrant -- /bin/true",
		"--user=qdrant -- /bin/true",
		"-iu qdrant -- /bin/true",
		"-uqdrant -- /bin/true",
	}
	for _, args := range drops {
		t.Run(args, func(t *testing.T) {
			line := privilegeLineUID(t, "privilege-raises", "__sys_setresuid", "/usr/bin/sudo", args, 0)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("privilege drop from root was forwarded as a raise")
			}
			if reason != "privilege_drop" {
				t.Fatalf("reason = %q, want privilege_drop", reason)
			}
		})
	}

	raises := []struct {
		args string
		uid  uint32
	}{
		{"-u root -- /bin/id", 0},        // target is root
		{"-i", 0},                        // root shell, no user switch
		{"-u qdrant -- /bin/true", 1000}, // caller was not root
	}
	for _, tc := range raises {
		t.Run("raise "+tc.args, func(t *testing.T) {
			line := privilegeLineUID(t, "privilege-raises", "__sys_setresuid", "/usr/bin/sudo", tc.args, tc.uid)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("sudo %q was suppressed as %q", tc.args, reason)
			}
		})
	}
}

func TestContainerWorkloadNoise_isSuppressed(t *testing.T) {
	cfg := testConfig()

	cases := []struct {
		name   string
		events string
		binary string
		args   string
		parent string
		reason string
	}{
		{"container health probe", "process_exec", "/bin/sh", `-c "curl --silent --fail http://localhost:8080/health | jq -n"`, "/usr/bin/tini", "health_check"},
		{"lifecycle healthcheck", "process_exec", "/usr/bin/bash", "/lifecycle/ak healthcheck", "/usr/bin/tini", "health_check"},
		{"false liveness probe", "process_exec", "/bin/sh", "-c -- /bin/false", "", "health_check"},
		{"cron.d wrapper", "process_exec", "/bin/sh", `-c "cd / && run-parts --report /etc/cron.hourly"`, "/usr/sbin/cron", "cron"},
		{"dpkg version compare", "process_exec", "/usr/bin/dpkg", "--compare-versions 1.0 lt 2.0", "/usr/bin/needrestart", "package_query"},
		{"dpkg version", "process_exec", "/usr/bin/dpkg", "--version", "/usr/bin/needrestart", "package_query"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg.events = tc.events
			line := privilegeLine(t, "no-policy", "no_function", tc.binary)
			if tc.events == "process_exec" {
				line = execLine(t, tc.binary, tc.args, tc.parent)
			}
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("expected container or package noise to be suppressed")
			}
			if reason != tc.reason {
				t.Fatalf("reason = %q, want %q", reason, tc.reason)
			}
		})
	}
}

func TestVolatileIdentifiersCollapseUnderDedup(t *testing.T) {
	cfg := testConfig()

	cases := []struct{ name, first, second string }{
		{
			name:   "ansible temp directory",
			first:  `/bin/sh -c "( umask 77 && mkdir -p /root/.ansible/tmp/ansible-tmp-1793194027.3013685-1620546-16158262709775 )"`,
			second: `/bin/sh -c "( umask 77 && mkdir -p /root/.ansible/tmp/ansible-tmp-1793194058.9370801-1622223-28773565743219 )"`,
		},
		{
			name:   "cpu usage sampler",
			first:  `/bin/sh -c "/home/dev/.vscode-server/server/out/vs/base/node/cpuUsage.sh" 2345934 2346105`,
			second: `/bin/sh -c "/home/dev/.vscode-server/server/out/vs/base/node/cpuUsage.sh" 2359120 2359121`,
		},
		{
			name:   "editor server launch",
			first:  `/usr/bin/sh /home/dev/.vscode-server/cli/servers/Stable-aaa/server/bin/code-server --agent-host-bridge-port=45713 --socket-path=/tmp/code-3ccebbf3-0280-42ad-befc-3566a4c6ed82 --start-server`,
			second: `/usr/bin/sh /home/dev/.vscode-server/cli/servers/Stable-aaa/server/bin/code-server --agent-host-bridge-port=39001 --socket-path=/tmp/code-86948d08-171f-4d27-8528-c3e6e3270ac1 --start-server`,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			first := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", tc.first)
			second := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", tc.second)
			if first.dedupKey() != second.dedupKey() {
				t.Fatalf("volatile identifiers defeat dedup:\n  %s\n  %s", tc.first, tc.second)
			}
			if first.dedupKey() == newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", "/usr/bin/systemctl restart nginx").dedupKey() {
				t.Fatal("distinct behaviours collapsed")
			}
		})
	}
}

func TestConnectionTokensAreRedacted(t *testing.T) {
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec",
		`process_exec: binary=/usr/bin/sh args=/home/dev/.vscode-server/server/bin/code-server --connection-token=s3cr3t-token-value --start-server`)
	got := a.Annotations["summary"]
	if strings.Contains(got, "s3cr3t-token-value") {
		t.Fatalf("connection token leaked into the summary: %s", got)
	}
	if !strings.Contains(got, "--connection-token=<redacted>") {
		t.Fatalf("token placeholder missing: %s", got)
	}
}

func TestCommandLineSecretsAreRedacted(t *testing.T) {
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec",
		`process_exec: binary=/bin/bash args=-c "P=$(base64 -d <<< 'c2VjcmV0LWJtYy1wYXNz') && ipmitool -I lanplus -H 203.0.113.10 -U admin -P '$P' mc info"`)
	got := a.Annotations["summary"]
	for _, secret := range []string{"c2VjcmV0LWJtYy1wYXNz", "'$P'"} {
		if strings.Contains(got, secret) {
			t.Fatalf("secret %q leaked into the summary: %s", secret, got)
		}
	}
	if !strings.Contains(got, "base64 -d <<< <redacted>") || !strings.Contains(got, "-P <redacted>") {
		t.Fatalf("redaction placeholders missing: %s", got)
	}
}

func TestAnsibleAndSnapNoiseIsSuppressed(t *testing.T) {
	cases := []struct {
		name   string
		events string
		line   func(t *testing.T) string
		reason string
	}{
		{
			name:   "ansible temp dir",
			events: "process_exec",
			line: func(t *testing.T) string {
				return execLine(t, "/bin/sh", `-c "( umask 77 && mkdir -p /root/.ansible/tmp/ansible-tmp-1.2-3-4 )"`, "/usr/bin/bash")
			},
			reason: "ansible_probe",
		},
		{
			name:   "ansible file transfer",
			events: "process_exec",
			line: func(t *testing.T) string {
				return execLine(t, "/usr/bin/bash", `-c "/usr/lib/openssh/sftp-server "`, "/usr/bin/ssh")
			},
			reason: "ansible_probe",
		},
		{
			name:   "snap application start",
			events: "process_kprobe",
			line: func(t *testing.T) string {
				return privilegeLine(t, "privilege-raises", "security_capset", "/snap/snapd/28254/usr/lib/snapd/snap-confine")
			},
			reason: "expected_privilege",
		},
		{
			name:   "samba session credential change",
			events: "process_kprobe",
			line: func(t *testing.T) string {
				return privilegeLine(t, "privilege-raises", "security_capset", "/usr/sbin/smbd")
			},
			reason: "expected_privilege",
		},
	}

	cfg := testConfig()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg.events = tc.events
			_, _, ok, reason := tetragonAlertDecision(tc.line(t), cfg)
			if ok {
				t.Fatal("expected routine event to be suppressed")
			}
			if reason != tc.reason {
				t.Fatalf("reason = %q, want %q", reason, tc.reason)
			}
		})
	}
}

func TestAnsibleSudoPrivilegeRaiseIsSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   "privilege-raises",
			"function_name": "security_capset",
			"process": map[string]any{
				"binary":    "/usr/bin/sudo",
				"arguments": `-H -S -n -u root /bin/sh -c "echo BECOME-SUCCESS-abc123 ; /usr/bin/python3"`,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, _, ok, reason := tetragonAlertDecision(string(line), cfg)
	if ok {
		t.Fatal("expected the Ansible become transition to be suppressed")
	}
	if reason != "ansible_probe" {
		t.Fatalf("reason = %q, want ansible_probe", reason)
	}

	// A sudo transition outside the become wrapper stays signal.
	line, err = json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   "privilege-raises",
			"function_name": "security_capset",
			"process":       map[string]any{"binary": "/usr/bin/sudo", "arguments": "-u root /bin/bash"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, ok, reason := tetragonAlertDecision(string(line), cfg); !ok {
		t.Fatalf("unrelated sudo transition was suppressed as %q", reason)
	}
}
