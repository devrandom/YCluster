package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"ycluster.local/ids-forwarder/sigma"
)

// hostNetTCPLine builds a container connect from a process that shares the
// host network namespace, as a podman/docker --network=host workload does.
func hostNetTCPLine(t *testing.T, pid uint32, started time.Time, daddr string, port int) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"function_name": "tcp_connect",
			"process": map[string]any{
				"binary":     "/usr/bin/curl",
				"arguments":  "http://127.0.0.1:8080/",
				"pid":        pid,
				"start_time": started.UTC().Format(time.RFC3339Nano),
				"ns": map[string]any{
					"pid": map[string]any{"inum": 4026532001},
					"mnt": map[string]any{"inum": 4026532002},
					"net": map[string]any{"inum": 4026531833, "is_host": true},
				},
			},
			"args": []any{
				map[string]any{
					"sock_arg": map[string]any{"saddr": "10.100.0.20", "daddr": daddr, "dport": port},
				},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestHostNetworkContainerLoopbackAlerts(t *testing.T) {
	root, pid, started := fakeContainerProc(t)
	withProcRoot(t, root)
	cfg := testConfig()
	cfg.events = "process_kprobe"

	a, _, ok, reason := tetragonAlertDecision(hostNetTCPLine(t, pid, started, "127.0.0.1", 8080), cfg)
	if !ok {
		t.Fatalf("host-network container -> host loopback was suppressed as %q", reason)
	}
	if a.Labels["layer"] != "container" || a.Labels["net"] != "host" {
		t.Fatalf("host-network container lost attribution: %v", a.Labels)
	}

	// Ordinary internet egress stays routine: sharing the host network does
	// not make a tenant's public traffic a cluster event.
	if _, _, ok, reason := tetragonAlertDecision(hostNetTCPLine(t, pid, started, "203.0.113.9", 443), cfg); ok || reason != "tenant_egress" {
		t.Fatalf("host-network container egress: ok=%v reason=%q, want tenant_egress", ok, reason)
	}
}

func TestEscalationFallsBackToParentExecID(t *testing.T) {
	resetExecCache()
	t.Cleanup(resetExecCache)

	body := &tetragonLog{
		Process: tetragonProcess{
			Binary:       "/usr/bin/sudo",
			PID:          100,
			UID:          0,
			ExecID:       "exec-sudo",
			ParentExecID: "exec-shell",
		},
	}
	rememberExec(&tetragonLog{
		Process: tetragonProcess{PID: 90, UID: 1000, ExecID: "exec-shell"},
	})
	if got := escalationSource(body); got != "execid" {
		t.Fatalf("escalation source = %q, want execid from the cached parent", got)
	}

	// A cached root parent is a positive answer, exactly like a rendered one.
	rememberExec(&tetragonLog{
		Process: tetragonProcess{PID: 91, UID: 0, ExecID: "exec-root-shell"},
	})
	body.Process.ParentExecID = "exec-root-shell"
	if got := escalationSource(body); got != "" {
		t.Fatalf("root invoker produced escalation source %q, want none", got)
	}
}

func TestSigmaEscalationUsesTheExecCache(t *testing.T) {
	resetExecCache()
	t.Cleanup(resetExecCache)

	rememberExec(&tetragonLog{
		Process: tetragonProcess{PID: 90, UID: 1000, ExecID: "exec-shell"},
		Parent:  &tetragonProc{PID: 90, UID: 1000, ExecID: "exec-shell"},
	})
	// The event the forwarder stored is the sudo itself: its parent uid comes
	// from the parent block of that event.
	rememberExec(&tetragonLog{
		Process: tetragonProcess{PID: 100, UID: 0, ExecID: "exec-sudo", Binary: "/usr/bin/sudo"},
		Parent:  &tetragonProc{PID: 90, UID: 1000, ExecID: "exec-shell"},
	})
	if got := sigmaEscalationSource(sigma.Event{Image: "/usr/bin/sudo", ExecId: "exec-sudo"}); got != "execid" {
		t.Fatalf("sigma escalation source = %q, want execid", got)
	}
}

func TestSigmaLayerFromTheExecCache(t *testing.T) {
	resetExecCache()
	t.Cleanup(resetExecCache)
	withProcRoot(t, t.TempDir())

	rememberExec(&tetragonLog{
		Process: tetragonProcess{
			PID:    700,
			UID:    1000123,
			ExecID: "exec-container",
			NS:     containerNS(),
		},
	})
	r := sigmaEventLayer(sigma.Event{ProcessId: 700, StartTime: "2026-01-01T00:00:00Z", User: "1000123", ExecId: "exec-container"})
	if !r.Resolved || r.Layer != "container" {
		t.Fatalf("sigma layer = %+v, want a container verdict from the cached exec", r)
	}
}

func TestExecCacheKeepsThePreviousGeneration(t *testing.T) {
	resetExecCache()
	t.Cleanup(resetExecCache)

	for i := 0; i < execCacheCap; i++ {
		execCacheStore(fmt.Sprintf("exec-%d", i), execEntry{uid: uint32(i)})
	}
	execCacheStore("overflow", execEntry{uid: 7})
	if _, ok := execLookup("exec-0"); !ok {
		t.Fatal("the oldest generation was evicted when the current one filled")
	}
	if entry, ok := execLookup("overflow"); !ok || entry.uid != 7 {
		t.Fatal("the current generation does not hold the newest entry")
	}
	if _, ok := execLookup(""); ok {
		t.Fatal("an empty exec id must never resolve")
	}
}

func TestWorldWritableCwdDrivesTheSummary(t *testing.T) {
	for _, dir := range []string{"/tmp", "/tmp/staging", "/var/tmp/payload", "/dev/shm/x", "/run/user/1000"} {
		if !worldWritableCwd(dir) {
			t.Fatalf("%s should read as world-writable", dir)
		}
	}
	for _, dir := range []string{"", "/", "/root", "/tmpfoo", "/var/tmpx"} {
		if worldWritableCwd(dir) {
			t.Fatalf("%s should not read as world-writable", dir)
		}
	}

	body := &tetragonLog{Process: tetragonProcess{Binary: "/usr/bin/curl", Cwd: "/var/tmp/stage"}}
	summary := eventSummaryRaw("process_exec", body)
	if !strings.Contains(summary, "cwd=/var/tmp/stage") {
		t.Fatalf("summary %q lost the staging directory", summary)
	}
}
