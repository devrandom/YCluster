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
		{"docker", "0::/system.slice/docker-abc123.scope", "", true},
		{"podman", "0::/machine.slice/libpod-abc123.scope", "", true},
		{"kube", "0::/kubepods.slice/kubepods-burstable.slice/cri-containerd-abc.scope", "", true},
		{"host", "0::/user.slice/user-0.slice/session-1.scope", "", false},
		{"init", "0::/init.scope", "", false},
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
}
