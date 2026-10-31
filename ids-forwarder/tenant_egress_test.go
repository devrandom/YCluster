package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeContainerProc builds a /proc tree whose single pid looks like a process
// inside Incus container "abdou2" started at the returned timestamp.
func fakeContainerProc(t *testing.T) (root string, pid uint32, started time.Time) {
	t.Helper()
	root = t.TempDir()
	pid = 4242
	const btime, ticks = int64(1700000000), uint64(12345)
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"),
		[]byte("0::/lxc.payload.abdou2/user.slice/session-1.scope\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stat := fmt.Sprintf("%d (curl) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", pid, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}
	return root, uint32(pid), time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
}

func containerTCPLine(t *testing.T, pid uint32, started time.Time, daddr string, port int) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"function_name": "tcp_connect",
			"process": map[string]any{
				"binary":     "/usr/bin/curl",
				"arguments":  "https://example.invalid/",
				"pid":        pid,
				"start_time": started.UTC().Format(time.RFC3339Nano),
			},
			"args": []any{
				map[string]any{
					"sock_arg": map[string]any{"daddr": daddr, "dport": port},
				},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func containerExecLine(t *testing.T, pid uint32, started time.Time, binary, args string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"binary":     binary,
				"arguments":  args,
				"pid":        pid,
				"start_time": started.UTC().Format(time.RFC3339Nano),
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestContainerInternalExecIsSuppressed(t *testing.T) {
	root, pid, started := fakeContainerProc(t)
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_exec"
	line := containerExecLine(t, pid, started, "/usr/bin/bash", "/home/tenant/serve-model")
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); ok || reason != "container_internal" {
		t.Fatalf("container exec: ok=%v reason=%q, want container_internal", ok, reason)
	}
}

func TestContainerPublicEgressIsSuppressed(t *testing.T) {
	root, pid, started := fakeContainerProc(t)
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_kprobe"
	if _, _, ok, reason := tetragonAlertDecision(containerTCPLine(t, pid, started, "203.0.113.10", 443), cfg); ok || reason != "tenant_egress" {
		t.Fatalf("container internet egress: ok=%v reason=%q, want suppressed as tenant_egress", ok, reason)
	}
}

func TestContainerClusterConnectStillAlerts(t *testing.T) {
	root, pid, started := fakeContainerProc(t)
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_kprobe"
	for _, dst := range []struct {
		addr string
		port int
	}{
		{"10.0.0.12", 2381}, // etcd
		{"10.0.0.20", 22},   // another cluster host
		{"192.168.66.5", 22},
	} {
		a, _, ok, reason := tetragonAlertDecision(containerTCPLine(t, pid, started, dst.addr, dst.port), cfg)
		if !ok {
			t.Fatalf("container -> %s:%d was suppressed as %q", dst.addr, dst.port, reason)
		}
		if a.Labels["layer"] != "container" || a.Labels["container"] != "abdou2" {
			t.Fatalf("container -> %s:%d lost attribution: %v", dst.addr, dst.port, a.Labels)
		}
	}
}

func TestContainerBridgeTrafficIsClassified(t *testing.T) {
	root, pid, started := fakeContainerProc(t)
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_kprobe"
	for _, dst := range []struct {
		addr string
		port int
	}{
		{"10.100.0.7", 40001}, // peer instance on the Incus bridge
		{"172.17.0.5", 5432},  // docker0 peer
		{"127.0.0.1", 8000},   // the instance itself
	} {
		if _, _, ok, reason := tetragonAlertDecision(containerTCPLine(t, pid, started, dst.addr, dst.port), cfg); ok || reason != "container_bridge" {
			t.Fatalf("container -> %s:%d: ok=%v reason=%q, want container_bridge", dst.addr, dst.port, ok, reason)
		}
	}
}

func TestHostEgressIsNotSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	line := tcpLine(t, "/usr/bin/curl", "https://203.0.113.10/", "203.0.113.10", 8443)
	if _, _, ok, reason := tetragonAlertDecision(line, cfg); !ok {
		t.Fatalf("host egress was suppressed as %q", reason)
	}
}

func TestContainerExecIgnoresHostExecRules(t *testing.T) {
	old := procRoot
	procRoot = t.TempDir() // no /proc entries: attribution has to use the uid
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_exec"
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{
				"binary":    "/bin/sh",
				"arguments": `-c -- "/usr/bin/env -i PATH=/usr/bin run-parts --lsbsysinit /etc/update-motd.d"`,
				"uid":       1000000,
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, _, ok, reason := tetragonAlertDecision(string(line), cfg)
	if ok {
		t.Fatal("container exec must not forward")
	}
	if reason != "container_internal" {
		t.Fatalf("reason = %q, want container_internal", reason)
	}
}

func TestContainerSelfProbeIsSuppressedButLateralIsNot(t *testing.T) {
	old := procRoot
	procRoot = t.TempDir()
	defer func() { procRoot = old }()

	cfg := testConfig()
	cfg.events = "process_kprobe"
	line := func(binary, args string) string {
		t.Helper()
		out, err := json.Marshal(map[string]any{
			"process_kprobe": map[string]any{
				"function_name": "tcp_connect",
				"process": map[string]any{
					"binary":    binary,
					"arguments": args,
					"uid":       1000000,
				},
				"args": []any{map[string]any{
					"sock_arg": map[string]any{"daddr": "10.0.0.100", "dport": 443},
				}},
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		return string(out)
	}

	if _, _, ok, reason := tetragonAlertDecision(line("/app/vikunja/vikunja", "healthcheck"), cfg); ok || reason != "expected_tcp" {
		t.Fatalf("vikunja healthcheck: ok=%v reason=%q, want suppressed as expected_tcp", ok, reason)
	}
	if _, _, ok, _ := tetragonAlertDecision(line("/usr/bin/curl", "https://10.0.0.100/"), cfg); !ok {
		t.Fatal("a container reaching the same address with another binary must still alert")
	}
}
