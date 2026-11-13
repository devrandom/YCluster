package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func classifyLabels(labels map[string]string) alert {
	return alert{Labels: labels, Annotations: map[string]string{"summary": "x"}}
}

func TestClassifyAlert(t *testing.T) {
	cases := []struct {
		name     string
		labels   map[string]string
		class    string
		evidence bool
	}{
		{
			name:   "root shell on the host",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "origin": "interactive", "actor": "root"},
			class:  "root_exec", evidence: true,
		},
		{
			name:   "root exec from a unit",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "origin": "unit"},
			class:  "root_exec", evidence: true,
		},
		{
			name:   "root exec with no session resolved",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "origin": "unknown"},
			class:  "root_exec", evidence: true,
		},
		{
			name:   "sudo from a user session",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "origin": "interactive", "actor": "ops", "escalation": "nonroot"},
			class:  "escalation_nonroot",
		},
		{
			name:   "non-root shell",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "33"},
			class:  "exec_nonroot",
		},
		{
			name:   "non-root private connect",
			labels: map[string]string{"rule_id": "tcp-connect", "layer": "host", "kind": "connect", "uid": "33"},
			class:  "lateral_nonroot",
		},
		{
			name:   "root private connect",
			labels: map[string]string{"rule_id": "tcp-connect", "layer": "host", "kind": "connect", "uid": "0"},
			class:  "root_connect", evidence: true,
		},
		{
			name:   "container connect",
			labels: map[string]string{"rule_id": "tcp-connect", "layer": "container", "kind": "connect", "uid": "1000123"},
			class:  "lateral_container",
		},
		{
			name:   "privileged container connect keeps the tenant signal",
			labels: map[string]string{"rule_id": "tcp-connect", "layer": "container", "kind": "connect", "uid": "0"},
			class:  "lateral_container",
		},
		{
			name:   "privileged container exec is host root",
			labels: map[string]string{"rule_id": "process_exec", "layer": "container", "kind": "exec", "uid": "0"},
			class:  "privileged_container", evidence: true,
		},
		{
			name:   "unverified exec is not a privileged container",
			labels: map[string]string{"rule_id": "process_exec", "layer": "unverified", "kind": "exec", "uid": "1000"},
			class:  "container_other",
		},
		{
			name:   "root module load",
			labels: map[string]string{"rule_id": "kernel-modules", "layer": "host", "kind": "module", "uid": "0"},
			class:  "root_module", evidence: true,
		},
		{
			name:   "root privilege transition",
			labels: map[string]string{"rule_id": "privilege-raises", "layer": "host", "kind": "privilege", "uid": "0", "origin": "unit"},
			class:  "root_privilege", evidence: true,
		},
		{
			name:   "root file write",
			labels: map[string]string{"rule_id": "exec-and-sensitive-file-writes", "layer": "host", "kind": "write", "uid": "0"},
			class:  "root_file_write", evidence: true,
		},
		{
			name:   "root sigma hit",
			labels: map[string]string{"source": sourceSigma, "rule_id": "rule", "layer": "host", "kind": "sigma", "uid": "0"},
			class:  "root_sigma", evidence: true,
		},
		{
			name:   "non-root sigma hit",
			labels: map[string]string{"source": sourceSigma, "rule_id": "rule", "layer": "host", "kind": "sigma", "uid": "1000"},
			class:  "sigma_nonroot",
		},
		{
			name:   "container sigma hit stays tenant coverage",
			labels: map[string]string{"source": sourceSigma, "rule_id": "rule", "layer": "container", "kind": "sigma", "uid": "0"},
			class:  "sigma_nonroot",
		},
		{
			name:   "missing uid is not proof of root",
			labels: map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec"},
			class:  "exec_nonroot",
		},
		{
			name:   "incus api audit",
			labels: map[string]string{"rule_id": "container-api", "layer": "host", "kind": "exec", "uid": "0"},
			class:  "audit",
		},
		{
			name:   "bridge guard block",
			labels: map[string]string{"source": sourceCrowdSec, "rule_id": "vm-bridge-cluster-egress", "layer": "host"},
			class:  "audit",
		},
		{
			name:   "crowdsec scenario",
			labels: map[string]string{"source": sourceCrowdSec, "rule_id": "ssh_failed-auth", "layer": "host"},
			class:  "crowdsec",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			class, evidence := classifyAlert(classifyLabels(tc.labels))
			if class != tc.class || evidence != tc.evidence {
				t.Fatalf("classifyAlert = %q evidence=%v, want %q evidence=%v", class, evidence, tc.class, tc.evidence)
			}
		})
	}
}

func TestPageEligible(t *testing.T) {
	cases := []struct {
		name   string
		labels map[string]string
		want   bool
	}{
		{"escalation pages", map[string]string{"escalation": "nonroot"}, true},
		{"sigma medium pages", map[string]string{"source": sourceSigma, "layer": "host", "uid": "1000", "sigma_level": "medium"}, true},
		{"sigma low stays silent", map[string]string{"source": sourceSigma, "layer": "host", "uid": "1000", "sigma_level": "low"}, false},
		{"non-root exec waits for measurement", map[string]string{"layer": "host", "kind": "exec", "uid": "1000"}, false},
		{"container lateral waits for measurement", map[string]string{"layer": "container", "kind": "connect", "uid": "1000000"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := classifyLabels(tc.labels)
			class, evidence := classifyAlert(a)
			if got := pageEligible(a, class); got != tc.want {
				t.Fatalf("pageEligible(%s) = %v, want %v", class, got, tc.want)
			}
			if evidence {
				t.Fatalf("unexpected evidence class %q", class)
			}
		})
	}
}

func TestEscalationSource(t *testing.T) {
	cases := []struct {
		name string
		body *tetragonLog
		want string
	}{
		{
			name: "parent elevating from a login account",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999}, Parent: &tetragonProc{PID: 4242, UID: 2000}},
			want: "uid",
		},
		{
			name: "parent already root",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999}, Parent: &tetragonProc{PID: 4242, UID: 0}},
			want: "",
		},
		{
			name: "audit uid survives a parentless event",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999, Auid: 2000}},
			want: "auid",
		},
		{
			name: "unset audit uid and no session",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999, Auid: auidUnset}},
			want: "",
		},
		{
			name: "audit uid below the login base is a system account",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999, Auid: 42}},
			want: "",
		},
		{
			name: "a root parent overrides a non-root audit uid",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999, Auid: 2000}, Parent: &tetragonProc{PID: 4242, UID: 0}},
			want: "",
		},
		{
			name: "an idmapped parent is a tenant, not a host escalation",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999}, Parent: &tetragonProc{PID: 4242, UID: 1000123}},
			want: "",
		},
		{
			name: "audit uid in the idmap range is a tenant",
			body: &tetragonLog{Process: tetragonProcess{PID: 999999, Auid: 1000123}},
			want: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := escalationSource(tc.body); got != tc.want {
				t.Fatalf("escalationSource = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestEscalationSourceFallsBackToTheSession(t *testing.T) {
	root := t.TempDir()
	sessions := t.TempDir()
	restoreProc, restoreSessions := procRoot, sessionDir
	procRoot, sessionDir = root, sessions
	t.Cleanup(func() { procRoot, sessionDir = restoreProc, restoreSessions })

	start := writeFakeProcess(t, root, 9100, "0::/user.slice/user-1000.slice/session-88.scope\n")
	if err := os.WriteFile(filepath.Join(sessions, "88"),
		[]byte("USER=ops\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	body := &tetragonLog{Process: tetragonProcess{PID: 9100, StartTime: start, Auid: auidUnset}}
	if got := escalationSource(body); got != "session" {
		t.Fatalf("escalationSource = %q, want session", got)
	}
}

func TestEvidenceDoesNotConsumeTheDedupKey(t *testing.T) {
	cfg := testConfig()
	cfg.detectionsFile = filepath.Join(t.TempDir(), "detections.jsonl")
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	// Same binary, same host: without the class in the key the root copy
	// would occupy the shape and swallow the non-root one.
	root := classifyLabels(map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "binary": "bash"})
	f.enqueue(root)
	if depth := f.queue.depth(); depth != 0 {
		t.Fatalf("root exec must not be delivered; depth=%d", depth)
	}
	user := classifyLabels(map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "1000", "binary": "bash"})
	f.enqueue(user)
	if depth := f.queue.depth(); depth != 1 {
		t.Fatalf("non-root exec must be delivered after an evidence event; depth=%d", depth)
	}
}

func TestShadowCounterMeasuresSilentClasses(t *testing.T) {
	cfg := testConfig()
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	for i := 0; i < 3; i++ {
		f.enqueue(classifyLabels(map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "1000", "binary": "bash"}))
	}
	if got := f.stats.pageCandidateCounts()["exec_nonroot"]; got != 1 {
		t.Fatalf("page candidates = %d, want 1 (dedup and coalescing apply)", got)
	}
	if got := f.stats.evidenceCounts()["root_exec"]; got != 0 {
		t.Fatalf("evidence count = %d, want 0", got)
	}
}

func TestPageLabelMatchesDelivery(t *testing.T) {
	cfg := testConfig()
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	root := classifyLabels(map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0"})
	f.enqueue(root)
	if depth := f.queue.depth(); depth != 0 {
		t.Fatalf("evidence must not be queued; depth=%d", depth)
	}

	escalation := classifyLabels(map[string]string{"rule_id": "process_exec", "layer": "host", "kind": "exec", "uid": "0", "escalation": "nonroot"})
	f.enqueue(escalation)
	a, ok := f.queue.pop()
	if !ok {
		t.Fatal("escalation must be delivered")
	}
	if a.Labels["page"] != "yes" {
		t.Fatalf("page = %q, want yes", a.Labels["page"])
	}
	if a.Labels["class"] != "escalation_nonroot" {
		t.Fatalf("class label = %q", a.Labels["class"])
	}

	silent := classifyLabels(map[string]string{"rule_id": "tcp-connect", "layer": "container", "kind": "connect", "uid": "1000000"})
	f.enqueue(silent)
	a, ok = f.queue.pop()
	if !ok {
		t.Fatal("silent coverage must still be delivered")
	}
	if a.Labels["page"] != "no" {
		t.Fatalf("page = %q, want no", a.Labels["page"])
	}
}

func TestDetectionRecordCarriesClass(t *testing.T) {
	path := filepath.Join(t.TempDir(), "detections.jsonl")
	cfg := testConfig()
	cfg.events = "process_exec"
	cfg.detectionsFile = path
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.handleLine(rootExecLine(0, "/bin/bash", false)); err != nil {
		t.Fatal(err)
	}
	f.closeState()

	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var rec detectionRecord
	if err := json.Unmarshal(body, &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Class != "root_exec" || rec.Delivery != deliveryEvidence {
		t.Fatalf("record class/delivery = %q/%q", rec.Class, rec.Delivery)
	}
	if rec.UID != "0" {
		t.Fatalf("record uid = %q, want 0", rec.UID)
	}
}
