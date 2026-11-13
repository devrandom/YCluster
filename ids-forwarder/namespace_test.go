package main

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

func hostNS() *eventNamespaces {
	return &eventNamespaces{
		Pid: tetragonNamespace{Inum: 4026531836, IsHost: true},
		Mnt: tetragonNamespace{Inum: 4026531841, IsHost: true},
	}
}

func containerNS() *eventNamespaces {
	return &eventNamespaces{
		Pid: tetragonNamespace{Inum: 4026532001},
		Mnt: tetragonNamespace{Inum: 4026532002},
	}
}

func writeCgroup(t *testing.T, root string, pid uint32, cgroup string) {
	t.Helper()
	dir := filepath.Join(root, fmt.Sprint(pid))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "cgroup"), []byte(cgroup+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLayerFromNamespaceSnapshot(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	const ticks = 4242
	start := fakeProcEntry(t, root, 700, btime, ticks, []string{"/bin/sh"}, "")
	withProcRoot(t, root)
	writeCgroup(t, root, 700, "0::/lxc.payload.tenant/init.scope")
	startText := start.Format(time.RFC3339Nano)

	cases := []struct {
		name      string
		pid       uint32
		start     string
		uid       uint32
		ns        *eventNamespaces
		layer     string
		source    string
		container string
	}{
		{"host namespace wins without /proc", 900, startText, 0, hostNS(), "", "namespace", ""},
		{
			name:  "pid namespace with the host mount namespace has host blast radius",
			pid:   900,
			start: startText,
			uid:   0,
			ns: &eventNamespaces{
				Pid: tetragonNamespace{Inum: 4026532003},
				Mnt: tetragonNamespace{Inum: 4026531841, IsHost: true},
			},
			layer: "", source: "namespace", container: "",
		},
		{"container snapshot with a live cgroup keeps the name", 700, startText, 1000000, containerNS(), "container", "namespace+cgroup", "tenant"},
		{"container snapshot, dead pid, idmapped uid", 901, startText, 1000123, containerNS(), "container", "namespace+uid", ""},
		{"container snapshot, dead pid, login uid is unverified", 902, startText, 1000, containerNS(), "unverified", "namespace+uid", ""},
		{"container snapshot, dead pid, uid 0 is unverified", 903, startText, 0, containerNS(), "unverified", "namespace+uid", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveProcessLayer(tc.pid, tc.start, tc.uid, tc.ns)
			if !got.Resolved || got.Layer != tc.layer || got.Source != tc.source || got.Name != tc.container {
				t.Fatalf("resolveProcessLayer = %+v, want layer %q source %q container %q", got, tc.layer, tc.source, tc.container)
			}
		})
	}
}

func TestLayerWithoutNamespaceKeepsTheProcPath(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	got := resolveProcessLayer(999, "", 0, nil)
	if got.Resolved {
		t.Fatalf("a pid-less, namespace-less process must stay unresolved: %+v", got)
	}
}

func TestEventLayerPrefersTheNamespaceVerdict(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	// The parent is a live host process; the child carries a container
	// snapshot. The event's own snapshot decides.
	const btime = int64(1700000000)
	parentStart := fakeProcEntry(t, root, 800, btime, 11, []string{"/usr/sbin/sshd"}, "")
	withProcRoot(t, root)
	writeCgroup(t, root, 800, "0::/system.slice/ssh.service")

	body := &tetragonLog{
		Process: tetragonProcess{
			PID: 801, UID: 1000123, StartTime: parentStart.Format(time.RFC3339Nano), NS: containerNS(),
		},
		Parent: &tetragonProc{
			PID: 800, StartTime: parentStart.Format(time.RFC3339Nano),
		},
	}
	layer, name, source := tetragonEventLayerInfo(body)
	if layer != "container" || source != "namespace+uid" {
		t.Fatalf("layer=%q name=%q source=%q, want container from the namespace", layer, name, source)
	}
}

func TestContainerCgroupBeatsAHostNamespaceVerdict(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	const btime = int64(1700000000)
	// A container running in the host pid namespace (--pid=host, or Incus
	// keep=pid): the snapshot says host, the cgroup names the workload. The
	// cgroup wins so the container keeps its suppression and its name.
	start := fakeProcEntry(t, root, 850, btime, 33, []string{"/bin/sh"}, "")
	withProcRoot(t, root)
	writeCgroup(t, root, 850, "0::/lxc.payload.pidhost/init.scope")

	got := resolveProcessLayer(850, start.Format(time.RFC3339Nano), 1000000, hostNS())
	if !got.Resolved || got.Layer != "container" || got.Name != "pidhost" || got.Source != "namespace+cgroup" {
		t.Fatalf("resolveProcessLayer = %+v, want the container from its cgroup", got)
	}
}

func TestSnapshotResolvesAProcessThatHasExited(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	resetProcessSnapshot()
	t.Cleanup(resetProcessSnapshot)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 4321, btime, 777,
		[]string{"/usr/bin/python3", "/opt/admin-services/health-api.py"}, "/usr/bin/python3.12")

	captureProcessSnapshot(nil)

	// The process exits: its /proc entry is gone, but the snapshot still
	// answers, which is the whole point of capturing at startup.
	if err := os.RemoveAll(filepath.Join(root, "4321")); err != nil {
		t.Fatal(err)
	}
	binary, args, ok := resolveProcessIdentity(4321, start.Format(time.RFC3339Nano))
	if !ok || binary != "/usr/bin/python3.12" || args != "/opt/admin-services/health-api.py" {
		t.Fatalf("resolveProcessIdentity from snapshot = %q %q %v", binary, args, ok)
	}
	if _, _, ok := resolveProcessIdentity(4321, start.Add(time.Minute).Format(time.RFC3339Nano)); ok {
		t.Fatal("a reused pid must not borrow the snapshot entry")
	}
}

func TestMemoAnswersAfterARestartReplay(t *testing.T) {
	root := t.TempDir()
	withProcRoot(t, root)
	resetProcessSnapshot()
	t.Cleanup(resetProcessSnapshot)
	const btime = int64(1700000000)
	start := fakeProcEntry(t, root, 4400, btime, 555, []string{"/bin/bash"}, "")

	// No snapshot: the first call reads live /proc and memoises the answer.
	if _, _, ok := resolveProcessIdentity(4400, start.Format(time.RFC3339Nano)); !ok {
		t.Fatal("live /proc should resolve the process")
	}
	if err := os.RemoveAll(filepath.Join(root, "4400")); err != nil {
		t.Fatal(err)
	}
	if binary, _, ok := resolveProcessIdentity(4400, start.Format(time.RFC3339Nano)); !ok || binary != "/bin/bash" {
		t.Fatalf("memo = %q %v, want the first answer", binary, ok)
	}
}

func TestProvenanceCounts(t *testing.T) {
	var m metrics
	m.countProvenance(eventProvenance{layer: "namespace"})
	m.countProvenance(eventProvenance{layer: "namespace", escalation: "uid"})
	m.countProvenance(eventProvenance{layer: "fallback"})
	got := m.provenanceCounts()
	want := map[string]int64{
		"layer\x00namespace": 2,
		"layer\x00fallback":  1,
		"escalation\x00uid":  1,
	}
	for key, value := range want {
		if got[key] != value {
			t.Fatalf("%q = %d, want %d (all: %v)", key, got[key], value, got)
		}
	}
}

func TestSigmaLayerParity(t *testing.T) {
	line := `{"time":"2026-11-13T17:00:00Z","process_exec":{"process":{"pid":7001,"uid":1000123,"auid":2000,"binary":"/bin/sh","arguments":"-c id","ns":{"pid":{"inum":4026532001},"mnt":{"inum":4026532002}}}}}`
	ev, ok, err := sigma.NormalizeLine(line)
	if err != nil || !ok {
		t.Fatalf("NormalizeLine = %v %v", ok, err)
	}
	resolveSigmaIdentity(ev)
	if ev.Layer != "container" {
		t.Fatalf("sigma layer = %q, want container from the namespace", ev.Layer)
	}
	if ev.Auid != 2000 {
		t.Fatalf("sigma auid = %d, want the event value", ev.Auid)
	}

	host := `{"time":"2026-11-13T17:00:01Z","process_exec":{"process":{"pid":7002,"uid":0,"binary":"/bin/sh","ns":{"pid":{"inum":4026531836,"is_host":true},"mnt":{"inum":4026531841,"is_host":true}}}}}`
	ev, ok, err = sigma.NormalizeLine(host)
	if err != nil || !ok {
		t.Fatalf("NormalizeLine = %v %v", ok, err)
	}
	resolveSigmaIdentity(ev)
	if ev.Layer != "host" {
		t.Fatalf("sigma layer = %q, want host", ev.Layer)
	}
}

func TestApplyIdentityEscalationSkipsOnlyContainers(t *testing.T) {
	body := &tetragonLog{
		Process: tetragonProcess{PID: 900, Binary: "/usr/bin/sudo"},
		Parent:  &tetragonProc{PID: 901, UID: 2000},
	}
	cases := []struct {
		layer string
		want  bool
	}{
		{"host", true},
		// An unverified layer is "we could not tell", not "tenant", and a
		// duplicate page is cheaper than a dropped escalation.
		{"unverified", true},
		{"container", false},
	}
	for _, tc := range cases {
		t.Run(tc.layer, func(t *testing.T) {
			a := newAlert(testConfig(), sourceTetragon, "warning", tc.layer, "process_exec", "x")
			applyIdentity(&a, "process_exec", body, tc.layer)
			got := a.Labels["escalation"] == "nonroot"
			if got != tc.want {
				t.Fatalf("escalation label = %q, want set=%v", a.Labels["escalation"], tc.want)
			}
			if tc.want && a.prov.escalation != "uid" {
				t.Fatalf("provenance = %q, want uid", a.prov.escalation)
			}
		})
	}
}

func TestApplyIdentityUnverifiedNeedsAnUndecidedLayer(t *testing.T) {
	body := &tetragonLog{
		Process: tetragonProcess{PID: 900, Binary: "/usr/bin/sudo"},
		Parent:  &tetragonProc{PID: 901, UID: 2000},
	}
	cases := []struct {
		source string
		want   bool
	}{
		// The cgroup positively named a user-scope runtime or a sandbox.
		{"cgroup", false},
		{"namespace+cgroup", false},
		// Nothing decided: the uid fallback did.
		{"uid", true},
		{"namespace+uid", true},
	}
	for _, tc := range cases {
		t.Run(tc.source, func(t *testing.T) {
			a := newAlert(testConfig(), sourceTetragon, "warning", "unverified", "process_exec", "x")
			a.prov.layer = tc.source
			applyIdentity(&a, "process_exec", body, "unverified")
			if got := a.Labels["escalation"] == "nonroot"; got != tc.want {
				t.Fatalf("escalation set = %v, want %v for source %q", got, tc.want, tc.source)
			}
		})
	}
}

func TestApplyIdentitySkipsNonExecAndNonTools(t *testing.T) {
	cfg := testConfig()
	privilege := &tetragonLog{
		PolicyName: "privilege-raises", FunctionName: "__sys_setuid",
		Process: tetragonProcess{PID: 900, Binary: "/usr/bin/sudo"},
		Parent:  &tetragonProc{PID: 901, UID: 2000},
	}
	a := newAlert(cfg, sourceTetragon, "warning", "host", "privilege-raises", "x")
	applyIdentity(&a, "process_kprobe", privilege, "host")
	if a.Labels["escalation"] != "" {
		t.Fatal("the credential commit must not duplicate the exec's escalation")
	}

	notATool := &tetragonLog{
		Process: tetragonProcess{PID: 900, Binary: "/bin/bash"},
		Parent:  &tetragonProc{PID: 901, UID: 2000},
	}
	b := newAlert(cfg, sourceTetragon, "warning", "host", "process_exec", "x")
	applyIdentity(&b, "process_exec", notATool, "host")
	if b.Labels["escalation"] != "" {
		t.Fatal("a copied binary must not inherit the escalation class")
	}
}
