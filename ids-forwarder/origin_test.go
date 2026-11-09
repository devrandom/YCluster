package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoginSessionID(t *testing.T) {
	cases := []struct {
		name   string
		cgroup string
		want   string
		ok     bool
	}{
		{"root login", "0::/user.slice/user-0.slice/session-2176.scope\n", "2176", true},
		{"nested", "0::/user.slice/user-1000.slice/session-42.scope/foo", "42", true},
		{"unit", "0::/system.slice/quadlet-metrics.service\n", "", false},
		// A container has session scopes of its own; processOriginFor
		// rejects the whole path before this parser runs.
		{"container", "0::/lxc.payload.web1/user.slice/user-1000.slice/session-1.scope", "1", true},
		{"not numeric", "0::/user.slice/user-0.slice/session-abc.scope", "", false},
		{"plain", "0::/init.scope", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := loginSessionID(tc.cgroup)
			if ok != tc.ok || got != tc.want {
				t.Fatalf("loginSessionID = %q, %v; want %q, %v", got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestUnitName(t *testing.T) {
	cases := []struct {
		cgroup string
		want   string
		ok     bool
	}{
		{"0::/system.slice/quadlet-metrics.service", "quadlet-metrics", true},
		{"0::/system.slice/cron.service", "cron", true},
		{"0::/system.slice/system-getty.slice/getty@tty1.service", "getty@tty1", true},
		{"0::/system.slice/system-dbus\\x2d1.slice/dbus.service", "dbus", true},
		// The deepest unit in the chain is the one running the process.
		{"0::/user.slice/user-0.slice/user@0.service/app.slice/x.scope", "x", true},
		{"0::/user.slice/user-0.slice/user@0.service", "user@0", true},
		{"0::/system.slice/run-r1a2b3c4d.scope", "", true},
		{"0::/init.scope", "", false},
		{"0::/user.slice/user-0.slice/session-9.scope", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.cgroup, func(t *testing.T) {
			got, ok := unitName(tc.cgroup)
			if ok != tc.ok || got != tc.want {
				t.Fatalf("unitName = %q, %v; want %q, %v", got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestProcessOriginForSession(t *testing.T) {
	root := t.TempDir()
	sessions := t.TempDir()
	restoreProc, restoreSessions := procRoot, sessionDir
	procRoot, sessionDir = root, sessions
	t.Cleanup(func() { procRoot, sessionDir = restoreProc, restoreSessions })

	start := writeFakeProcess(t, root, 9001, "0::/user.slice/user-0.slice/session-77.scope\n")
	if err := os.WriteFile(filepath.Join(sessions, "77"),
		[]byte("# private\nUID=0\nUSER=root\nSERVICE=sshd\nREMOTE=203.0.113.5 22\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	got := processOriginFor(9001, start)
	if got.Origin != "interactive" || got.Actor != "root" {
		t.Fatalf("origin = %+v, want interactive/root", got)
	}
}

func TestProcessOriginFallsBackToUnitName(t *testing.T) {
	root := t.TempDir()
	restoreProc := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = restoreProc })

	start := writeFakeProcess(t, root, 9002, "0::/system.slice/quadlet-metrics.service\n")
	got := processOriginFor(9002, start)
	if got.Origin != "unit" || got.Actor != "quadlet-metrics" {
		t.Fatalf("origin = %+v, want unit/quadlet-metrics", got)
	}
}

func TestProcessOriginIgnoresReusedPID(t *testing.T) {
	root := t.TempDir()
	restoreProc := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = restoreProc })

	// The event's start time is a minute before the process /proc describes.
	start := writeFakeProcess(t, root, 9003, "0::/user.slice/user-0.slice/session-5.scope\n")
	event := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339Nano)
	if event == start {
		t.Fatal("test timestamps collided")
	}
	if got := processOriginFor(9003, event); got.Origin != "unknown" {
		t.Fatalf("reused pid produced %+v, want unknown", got)
	}
}

func TestProcessOriginRejectsContainerSessionScopes(t *testing.T) {
	root := t.TempDir()
	restoreProc := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = restoreProc })

	start := writeFakeProcess(t, root, 9006,
		"0::/lxc.payload.web1/user.slice/user-1000.slice/session-1.scope\n")
	got := processOriginFor(9006, start)
	if got.Origin != "container" || got.Actor != "" {
		t.Fatalf("origin = %+v, want container with no host actor", got)
	}
}

func TestApplyOriginLabelsContainerLayer(t *testing.T) {
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "container", "process_kprobe", "process_kprobe: x")
	applyOrigin(&a, &tetragonLog{}, "container")
	if a.Labels["origin"] != "container" {
		t.Fatalf("origin = %q, want container", a.Labels["origin"])
	}
	if _, ok := a.Labels["actor"]; ok {
		t.Fatal("container alerts must not carry a host actor")
	}
}

func TestApplyOriginLabelsUnknownWhenUnresolvable(t *testing.T) {
	cfg := testConfig()
	a := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", "process_exec: x")
	applyOrigin(&a, &tetragonLog{Process: tetragonProcess{PID: 1, StartTime: "2024-01-01T00:00:00Z"}}, "host")
	if a.Labels["origin"] != "unknown" {
		t.Fatalf("origin = %q, want unknown", a.Labels["origin"])
	}
}

func TestDedupKeyIgnoresProvenance(t *testing.T) {
	cfg := testConfig()
	first := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", "process_exec: x")
	second := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", "process_exec: x")
	first.Labels["origin"], first.Labels["actor"] = "interactive", "root"
	second.Labels["origin"], second.Labels["actor"] = "unit", "cron"
	if first.dedupKey() != second.dedupKey() {
		t.Fatal("provenance must not split an alert into a second one")
	}
}

func TestEventOriginUsesParentWhenChildExited(t *testing.T) {
	root := t.TempDir()
	restoreProc := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = restoreProc })

	parentStart := writeFakeProcess(t, root, 9004, "0::/system.slice/cron.service\n")
	got := eventOrigin(tetragonProcess{PID: 9099, StartTime: "2024-01-01T00:00:00Z"},
		&tetragonProc{PID: 9004, StartTime: parentStart})
	if got.Origin != "unit" || got.Actor != "cron" {
		t.Fatalf("origin = %+v, want unit/cron", got)
	}
}

func TestAlertCarriesOriginLabels(t *testing.T) {
	root := t.TempDir()
	restoreProc := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = restoreProc })

	start := writeFakeProcess(t, root, 9005, "0::/system.slice/cron.service\n")
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"binary":     "/bin/bash",
				"arguments":  `-c "id"`,
				"pid":        9005,
				"start_time": start,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	cfg := testConfig()
	cfg.events = "process_exec"
	a, _, ok, reason := tetragonAlertDecision(string(line), cfg)
	if !ok {
		t.Fatalf("alert suppressed as %q", reason)
	}
	if a.Labels["origin"] != "unit" || a.Labels["actor"] != "cron" {
		t.Fatalf("labels = %v, want origin=unit actor=cron", a.Labels)
	}
}

// writeFakeProcess lays out /proc/<pid>/{cgroup,stat} plus the fake btime and
// returns the start_time the forwarder must see for this pid.
func writeFakeProcess(t *testing.T, root string, pid uint32, cgroup string) string {
	t.Helper()
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"), []byte(cgroup), 0o644); err != nil {
		t.Fatal(err)
	}
	const btime = int64(1700000000)
	const ticks = 12345
	stat := fmt.Sprintf("%d (bash) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", pid, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}
	return time.Unix(btime, 0).Add(ticks * time.Second / 100).UTC().Format(time.RFC3339Nano)
}
