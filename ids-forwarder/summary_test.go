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
	}

	for _, tc := range cases {
		line := execLine(t, tc.binary, tc.args, tc.parent)
		if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
			t.Fatalf("%s %q was suppressed as %q", tc.binary, tc.args, reason)
		}
	}
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
