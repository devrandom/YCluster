package main

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestContainerNameFromCgroup(t *testing.T) {
	cases := []struct {
		name   string
		cgroup string
		want   string
		ok     bool
	}{
		{"incus", "0::/lxc.payload.abdou2/user.slice/user-1000.slice/session-1.scope", "abdou2", true},
		{"lxc", "0::/lxc/web1/init.scope", "web1", true},
		{"docker", "0::/system.slice/docker-" + dockerID + ".scope", "", true},
		{"podman", "0::/machine.slice/libpod-" + dockerID + ".scope", "", true},
		{"kube", "0::/kubepods.slice/kubepods-burstable.slice/cri-containerd-abc.scope", "", true},
		{"docker v1", "0::/docker/" + dockerID, "", true},
		{"podman v1", "0::/libpod/" + dockerID, "", true},
		{"rootless docker shape", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/docker-" + dockerID + ".scope", "", true},
		{"forged named docker scope", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/docker-evil.scope", "", false},
		{"rootless kubepods shape", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/kubepods-evil.slice", "", true},
		{"user scope with kubepods name", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/kubepods-evil.scope", "", false},
		{"host", "0::/user.slice/user-0.slice/session-1.scope", "", false},
		{"init", "0::/init.scope", "", false},
		{"nested lxc name", "0::/system.slice/lxc.payload.evil.scope", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := containerNameFromCgroup(tc.cgroup)
			if ok != tc.ok || got != tc.want {
				t.Fatalf("containerNameFromCgroup = %q, %v; want %q, %v", got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestContainerForProcessChecksStartTime(t *testing.T) {
	root := t.TempDir()
	pid := uint32(4242)
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"),
		[]byte("0::/lxc.payload.abdou2/user.slice/session-1.scope\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	const btime = int64(1700000000)
	const ticks = 12345
	// /proc/<pid>/stat: state first, starttime is field 22 (index 19 after
	// the comm field).
	stat := fmt.Sprintf("%d (bash) S "+
		"1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", pid, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}

	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	want := time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
	if name, ok := containerForProcess(pid, want.Format(time.RFC3339Nano)); !ok || name != "abdou2" {
		t.Fatalf("containerForProcess = %q, %v; want abdou2, true", name, ok)
	}
	if _, ok := containerForProcess(pid, want.Add(10*time.Second).Format(time.RFC3339Nano)); ok {
		t.Fatal("reused pid must not be attributed to a container")
	}
	if _, ok := containerForProcess(pid, ""); ok {
		t.Fatal("missing start time must not be attributed")
	}
}

func TestContainerForEventFallsBackToParent(t *testing.T) {
	root := t.TempDir()
	const btime = int64(1700000000)
	const ticks = 999
	parentPID := uint32(5150)
	dir := filepath.Join(root, fmt.Sprint(parentPID))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"),
		[]byte("0::/lxc.payload.bastion/system.slice/ssh.service\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stat := fmt.Sprintf("%d (sshd) S "+
		"1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", parentPID, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}

	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	start := time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
	body := &tetragonLog{
		Process: tetragonProcess{PID: 6001, StartTime: start.Add(time.Second).Format(time.RFC3339Nano)},
		Parent:  &tetragonProc{PID: parentPID, StartTime: start.Format(time.RFC3339Nano)},
	}
	if name, ok := containerForEvent(body); !ok || name != "bastion" {
		t.Fatalf("containerForEvent = %q, %v; want bastion, true", name, ok)
	}

	body.Parent.StartTime = start.Add(30 * time.Second).Format(time.RFC3339Nano)
	if name, ok := containerForEvent(body); ok {
		t.Fatalf("reused parent pid must not be attributed to a container, got %q", name)
	}
}

func TestContainerForEventUsesIncusIDMap(t *testing.T) {
	old := procRoot
	procRoot = t.TempDir()
	defer func() { procRoot = old }()

	container := &tetragonLog{Process: tetragonProcess{PID: 7001, UID: 1000000,
		StartTime: "2026-10-30T19:21:08.494766950Z"}}
	if _, ok := containerForEvent(container); !ok {
		t.Fatal("idmapped uid must be attributed to a container")
	}
	host := &tetragonLog{Process: tetragonProcess{PID: 7002, UID: 0,
		StartTime: "2026-10-30T19:21:08.494766950Z"}}
	if _, ok := containerForEvent(host); ok {
		t.Fatal("uid 0 without a proc entry must stay host")
	}
	parent := &tetragonLog{
		Process: tetragonProcess{PID: 7003, UID: 0, StartTime: "2026-10-30T19:21:08.494766950Z"},
		Parent:  &tetragonProc{PID: 7004, UID: 1001001},
	}
	if _, ok := containerForEvent(parent); !ok {
		t.Fatal("idmapped parent uid must be attributed to a container")
	}
	subuid := &tetragonLog{Process: tetragonProcess{PID: 7005, UID: 100000,
		StartTime: "2026-10-30T19:21:08.494766950Z"}}
	if _, ok := containerForEvent(subuid); ok {
		t.Fatal("an ordinary host subuid must not be attributed to a container")
	}
	if _, layer := containerForEventLayer(subuid); layer != "unverified" {
		t.Fatalf("ordinary non-root uid layer = %q, want unverified", layer)
	}
	// A dead system-account process is a host service, not a rootless
	// container: `_apt` and PCP's `pcp` account exit before the line is read.
	systemAccount := &tetragonLog{Process: tetragonProcess{PID: 7006, UID: 42,
		StartTime: "2026-10-30T19:21:08.494766950Z"}}
	if _, layer := containerForEventLayer(systemAccount); layer != "" {
		t.Fatalf("dead system-account uid layer = %q, want host", layer)
	}
}

const dockerID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func TestRootlessUserScopeStaysHost(t *testing.T) {
	root := t.TempDir()
	const btime = int64(1700000000)
	const ticks = 4242
	pid := uint32(8888)
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"),
		[]byte("0::/user.slice/user-1000.slice/user@1000.service/app.slice/libpod-"+dockerID+".scope\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stat := fmt.Sprintf("%d (bash) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 %d 0\n", pid, ticks)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stat"),
		[]byte(fmt.Sprintf("cpu 0 0\nbtime %d\n", btime)), 0o644); err != nil {
		t.Fatal(err)
	}
	old := procRoot
	procRoot = root
	defer func() { procRoot = old }()

	want := time.Unix(btime, 0).Add(time.Duration(ticks) * time.Second / 100)
	if _, layer := containerForProcessLayer(pid, want.Format(time.RFC3339Nano)); layer != "unverified" {
		t.Fatalf("layer = %q, want unverified", layer)
	}
	if _, ok := containerForProcess(pid, want.Format(time.RFC3339Nano)); ok {
		t.Fatal("an unverified user-scope runtime name must not be a verified container")
	}
}
