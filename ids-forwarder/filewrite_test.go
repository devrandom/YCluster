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
