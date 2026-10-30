package main

import (
	"encoding/json"
	"testing"
)

func TestRoutineWireGuardWriteIsSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	line := fileWriteLine(t, "/usr/local/bin/ycluster", "/usr/local/bin/ycluster wg reconcile --up", "/etc/wireguard/wg0.conf")
	_, _, ok, reason := tetragonAlertDecision(line, cfg)
	if ok {
		t.Fatal("expected WireGuard reconciliation write to be suppressed")
	}
	if reason != "expected_file_write" {
		t.Fatalf("reason = %q, want expected_file_write", reason)
	}
}

func TestBlackboxTargetTempWriteIsSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	line := fileWriteLine(t, "/usr/bin/python3", "/usr/local/lib/cluster/update-blackbox-targets", "/etc/prometheus/tmpmytny")
	_, _, ok, reason := tetragonAlertDecision(line, cfg)
	if ok {
		t.Fatal("expected blackbox-targets temp write to be suppressed")
	}
	if reason != "expected_file_write" {
		t.Fatalf("reason = %q, want expected_file_write", reason)
	}

	// The rename target and other writers stay alertable.
	line = fileWriteLine(t, "/usr/bin/python3", "/usr/local/lib/cluster/update-blackbox-targets", "/etc/prometheus/file_sd/targets.json")
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
		t.Fatalf("real target write was suppressed as %q", reason)
	}
}

func TestOtherFileWritesAreForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct {
		name   string
		binary string
		args   string
		path   string
	}{
		{"different writer", "/usr/bin/python3", "/usr/local/bin/ycluster wg reconcile --up", "/etc/wireguard/wg0.conf"},
		{"different path", "/usr/local/bin/ycluster", "/usr/local/bin/ycluster wg reconcile --up", "/etc/ssh/sshd_config"},
		{"different ycluster command", "/usr/local/bin/ycluster", "/usr/local/bin/ycluster wg set", "/etc/wireguard/wg0.conf"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := fileWriteLine(t, tc.binary, tc.args, tc.path)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("unexpected file write was suppressed as %q", reason)
			}
		})
	}
}

func fileWriteLine(t *testing.T, binary, args, path string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   "exec-and-sensitive-file-writes",
			"function_name": "security_file_permission",
			"process":       map[string]any{"binary": binary, "arguments": args},
			"args":          []any{map[string]any{"file_arg": map[string]any{"path": path}}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestDaemonConfigWritesAreSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct{ binary, args, path string }{
		{"/usr/bin/pmiectl", "/usr/bin/pmiectl -m check", "/etc/pcp/pmie/lock"},
		{"/usr/bin/pmlogctl", "/usr/bin/pmlogctl -m check", "/etc/pcp/pmlogger/lock"},
		{"/usr/sbin/iptables-save", "", "/etc/iptables/rules.v4"},
		{"/usr/sbin/cupsd", "-l", "/etc/cups/cupsd.conf"},
	}
	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			line := fileWriteLine(t, tc.binary, tc.args, tc.path)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("expected daemon config write to be suppressed")
			}
			if reason != "expected_file_write" {
				t.Fatalf("reason = %q, want expected_file_write", reason)
			}
		})
	}

	// A different writer touching the same paths is still signal.
	for _, tc := range []struct{ binary, path string }{
		{"/bin/sh", "/etc/iptables/rules.v4"},
		{"/tmp/evil", "/etc/pcp/pmie/lock"},
	} {
		t.Run("other writer "+tc.path, func(t *testing.T) {
			line := fileWriteLine(t, tc.binary, "", tc.path)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("unexpected writer was suppressed as %q", reason)
			}
		})
	}
}

func TestZulipPushHostsWritesAreSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct{ binary, args, path string }{
		{"/usr/local/lib/cluster/setup-zulip-egress", "/usr/local/lib/cluster/setup-zulip-egress", "/etc/zulip/push-hosts.5uEW6O"},
		{"/usr/local/lib/cluster/setup-zulip-egress", "/usr/local/lib/cluster/setup-zulip-egress", "/etc/zulip/push-hosts"},
		{"/usr/bin/cat", "/etc/zulip/push-hosts.hNNBma", "/etc/zulip/push-hosts"},
	}
	for _, tc := range cases {
		line := fileWriteLine(t, tc.binary, tc.args, tc.path)
		_, _, ok, reason := tetragonAlertDecision(line, cfg)
		if ok {
			t.Fatalf("%s write was forwarded", tc.binary)
		}
		if reason != "expected_file_write" {
			t.Fatalf("reason = %q, want expected_file_write", reason)
		}
	}

	for _, tc := range []struct{ name, binary, args, path string }{
		{"other writer", "/bin/sh", "", "/etc/zulip/push-hosts"},
		{"other zulip path", "/usr/local/lib/cluster/setup-zulip-egress", "", "/etc/zulip/zulip.conf"},
		{"unrelated cat", "/usr/bin/cat", "/etc/ssh/sshd_config", "/etc/zulip/push-hosts"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			line := fileWriteLine(t, tc.binary, tc.args, tc.path)
			if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
				t.Fatalf("unexpected write was suppressed as %q", reason)
			}
		})
	}
}
