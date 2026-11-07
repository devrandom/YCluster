package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

// fakeProcEntry creates a /proc entry for pid and returns the exec time the
// sensor would have recorded for it.
func fakeProcEntry(t *testing.T, root string, pid uint32, btime int64, ticks uint64, argv []string, exe string) time.Time {
	t.Helper()
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}
	stat := fmt.Sprintf("%d (python3) S "+
		"1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", pid, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cmdline"),
		[]byte(strings.Join(argv, "\x00")+"\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	if exe != "" {
		if err := os.Symlink(exe, filepath.Join(dir, "exe")); err != nil {
			t.Fatal(err)
		}
	}
	return time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
}

func withProcRoot(t *testing.T, root string) {
	t.Helper()
	old := procRoot
	procRoot = root
	t.Cleanup(func() { procRoot = old })
}

func TestResolveProcessIdentityFromProc(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 4242, btime, 1000,
		[]string{"/usr/bin/python3", "/opt/admin-services/health-api.py"},
		"/usr/bin/python3.12")

	binary, args, ok := resolveProcessIdentity(4242, start.Format(time.RFC3339Nano))
	if !ok || binary != "/usr/bin/python3.12" || args != "/opt/admin-services/health-api.py" {
		t.Fatalf("resolveProcessIdentity = %q, %q, %v", binary, args, ok)
	}

	if _, _, ok := resolveProcessIdentity(4242, start.Add(10*time.Second).Format(time.RFC3339Nano)); ok {
		t.Fatal("a reused pid must not be resolved")
	}
	if _, _, ok := resolveProcessIdentity(9999, start.Format(time.RFC3339Nano)); ok {
		t.Fatal("a missing process must not be resolved")
	}
}

func TestResolveProcessIdentityWithoutExeLink(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 5150, btime, 1000,
		[]string{"/usr/bin/python3", "/opt/admin-services/app.py"}, "")

	binary, args, ok := resolveProcessIdentity(5150, start.Format(time.RFC3339Nano))
	if !ok || binary != "/usr/bin/python3" || args != "/opt/admin-services/app.py" {
		t.Fatalf("resolveProcessIdentity = %q, %q, %v", binary, args, ok)
	}
}

func TestUnknownExecIdentityIsResolvedBeforeSuppression(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 4718, btime, 1000,
		[]string{"/usr/bin/python3", "/opt/admin-services/health-api.py"},
		"/usr/bin/python3.12")

	cfg := testConfig()
	cfg.events = "process_kprobe"
	line := func(port int) string {
		out, err := json.Marshal(map[string]any{
			"process_kprobe": map[string]any{
				"function_name": "tcp_connect",
				"process": map[string]any{
					"binary":     unknownExecBinary,
					"arguments":  unknownExecArg,
					"pid":        4718,
					"uid":        0,
					"start_time": start.Format(time.RFC3339Nano),
				},
				"args": []any{map[string]any{
					"sock_arg": map[string]any{"daddr": "10.0.0.11", "dport": port},
				}},
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		return string(out)
	}

	if _, _, ok, reason := tetragonAlertDecision(line(2381), cfg); ok || reason != "expected_tcp" {
		t.Fatalf("resolved admin service to etcd: ok=%v reason=%q, want expected_tcp", ok, reason)
	}
	if _, _, ok, _ := tetragonAlertDecision(line(4443), cfg); !ok {
		t.Fatal("the resolved identity must still alert on an unexpected port")
	}
}

func TestResolveSigmaIdentity(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 4718, btime, 1000,
		[]string{"/usr/bin/python3", "/opt/admin-services/health-api.py"},
		"/usr/bin/python3.12")

	ev := &sigma.Event{
		Image:       unknownExecBinary,
		CommandLine: unknownExecBinary + " " + unknownExecArg,
		ProcessId:   4718,
		StartTime:   start.Format(time.RFC3339Nano),
	}
	resolveSigmaIdentity(ev)
	if ev.Image != "/usr/bin/python3.12" ||
		ev.CommandLine != "/usr/bin/python3.12 /opt/admin-services/health-api.py" {
		t.Fatalf("resolved event = %+v", ev)
	}
}
