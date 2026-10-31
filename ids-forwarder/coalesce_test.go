package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func incusForkfileLine(t *testing.T) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"policy_name":   "privilege-raises",
			"function_name": "security_task_fix_setuid",
			"process": map[string]any{
				"binary":    "/usr/libexec/incus/incusd",
				"arguments": "forkfile -- 3 4 5 2750",
			},
			"args": []any{map[string]any{"int_arg": 0}},
		},
		"time": time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestIncusForkfileGetsOwnRuleID(t *testing.T) {
	evType, _, body := eventType(incusForkfileLine(t))
	if evType == "" || body == nil {
		t.Fatal("event did not parse")
	}
	if got := tetragonRule(evType, body); got != "container-exec" {
		t.Fatalf("rule id = %q, want container-exec", got)
	}
}

func TestOtherPrivilegeRaiseKeepsPolicyRule(t *testing.T) {
	line := `{"process_kprobe":{"policy_name":"privilege-raises","function_name":"security_task_fix_setuid",
		"process":{"binary":"/usr/bin/sudo","arguments":"-i"},"args":[{"int_arg":0}]},
		"time":"2026-10-28T11:14:06Z"}`
	evType, _, body := eventType(line)
	if got := tetragonRule(evType, body); got != "privilege-raises" {
		t.Fatalf("rule id = %q, want privilege-raises", got)
	}
}

func TestIncusClientGetsOwnRuleID(t *testing.T) {
	line := `{"process_exec":{"process":{"binary":"/usr/bin/incus","arguments":"file pull bastion/etc/hosts -"}},
		"time":"2026-10-29T23:00:00Z"}`
	evType, _, body := eventType(line)
	if evType == "" || body == nil {
		t.Fatal("event did not parse")
	}
	if got := tetragonRule(evType, body); got != "container-api" {
		t.Fatalf("rule id = %q, want container-api", got)
	}
}

func TestRecurringContainerExecIsCoalesced(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	line := incusForkfileLine(t)
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after first event = %d, want 1", got)
	}

	// Six minutes later: past the shared 5m dedup window, still inside the
	// one-hour container-exec window, which is what paged every five
	// minutes before.
	key := ""
	for k := range f.dedup.keys {
		key = k
	}
	if key == "" {
		t.Fatal("first event was not marked in dedup")
	}
	f.dedup.keys[key] = time.Now().Add(-6 * time.Minute)

	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after repeat = %d, want 1 (coalesced)", got)
	}
	if got := f.stats.coalesced.Load(); got != 1 {
		t.Fatalf("coalesced = %d, want 1", got)
	}
	if got := f.stats.deduped.Load(); got != 0 {
		t.Fatalf("deduped = %d, want 0 (the repeat is outside the dedup window)", got)
	}

	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 2 {
		t.Fatalf("recorded %d detections, want 2 (coalescing must not lose the record)", n)
	}
}

func splitLines(s string) []string {
	var out []string
	for _, l := range trimSplit(s) {
		if l != "" {
			out = append(out, l)
		}
	}
	return out
}

func trimSplit(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

func TestIncusClientCommandsShareADedupKey(t *testing.T) {
	mk := func(args string) alert {
		return alert{
			Labels: map[string]string{
				"alertname": alertName,
				"source":    sourceTetragon,
				"severity":  "warning",
				"host":      "node",
				"layer":     "host",
				"rule_id":   "container-api",
			},
			Annotations: map[string]string{"summary": "process_exec: binary=/usr/bin/incus args=" + args},
		}
	}
	if mk("file pull bastion/etc/hosts -").dedupKey() != mk("info bastion").dedupKey() {
		t.Fatal("container-api alerts should collapse across subcommands")
	}
}

func incusClientLine(t *testing.T, args string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{"binary": "/usr/bin/incus", "arguments": args},
		},
		"time": time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestContainerAPIUsageIsAlwaysRecorded(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_exec"
	cfg.execBinaries = "incus"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	for _, args := range []string{"exec gw1 -- id", "restart gw2"} {
		if err := f.handleLine(incusClientLine(t, args)); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth = %d, want 1 alert for the pair", got)
	}
	if got := f.stats.deduped.Load(); got != 1 {
		t.Fatalf("deduped = %d, want 1", got)
	}
	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 2 {
		t.Fatalf("recorded %d detections, want both API calls", n)
	}
}

func shellExecLine(t *testing.T, cmd string) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_exec": map[string]any{
			"process": map[string]any{"binary": "/bin/sh", "arguments": cmd},
			"parent":  map[string]any{"binary": "/usr/sbin/sshd", "arguments": "-D"},
		},
		"time": time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestShellExecCoalescesButKeepsEveryRecord(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_exec"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	if err := f.handleLine(shellExecLine(t, `-c "echo one"`)); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after first shell = %d, want 1", got)
	}
	// A different command from the same host must not page again, but it
	// still has to appear in the detection history.
	if err := f.handleLine(shellExecLine(t, `-c "echo two"`)); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after second shell = %d, want 1 (same key)", got)
	}
	if got := f.stats.deduped.Load(); got != 1 {
		t.Fatalf("deduped = %d, want 1", got)
	}
	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 2 {
		t.Fatalf("recorded %d detections, want 2 (every shell command is evidence)", n)
	}
}

func TestTCPConnectKeyUsesDestinationOnly(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	alertFor := func(daddr string, port int) alert {
		t.Helper()
		a, ok := tetragonAlert(tcpLine(t, "/usr/bin/curl", "https://service/", daddr, port), cfg)
		if !ok {
			t.Fatalf("connect to %s:%d must alert", daddr, port)
		}
		return a
	}
	if alertFor("203.0.113.10", 443).dedupKey() != alertFor("203.0.113.10", 443).dedupKey() {
		t.Fatal("repeats to the same destination must share a key")
	}
	if alertFor("203.0.113.10", 443).dedupKey() == alertFor("203.0.113.11", 443).dedupKey() {
		t.Fatal("a different destination must produce a different key")
	}
}

func TestContainerExecCoalescesAcrossInvocations(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	alertFor := func(args string) alert {
		t.Helper()
		line, err := json.Marshal(map[string]any{
			"process_kprobe": map[string]any{
				"policy_name":   "privilege-raises",
				"function_name": "security_task_fix_setuid",
				"process": map[string]any{
					"binary":    "/usr/libexec/incus/incusd",
					"arguments": args,
				},
				"args": []any{map[string]any{"int_arg": 0}},
			},
			"time": time.Now().UTC().Format(time.RFC3339Nano),
		})
		if err != nil {
			t.Fatal(err)
		}
		a, ok := tetragonAlert(string(line), cfg)
		if !ok {
			t.Fatalf("forkfile %q must alert", args)
		}
		return a
	}
	if alertFor("forkfile -- 3 4 5 2750").dedupKey() != alertFor("forkfile -- 7 8 9 9999").dedupKey() {
		t.Fatal("forkfile invocations differ only by pid and must share one key")
	}
}

func ageDedupKeys(f *forwarder, age time.Duration) {
	f.dedup.mu.Lock()
	defer f.dedup.mu.Unlock()
	for k := range f.dedup.keys {
		f.dedup.keys[k] = time.Now().Add(-age)
	}
}

func TestShellExecRepeatWindowHoldsAnHour(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_exec"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	if err := f.handleLine(shellExecLine(t, `-c "echo one"`)); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after first shell = %d, want 1", got)
	}

	// Six minutes later: past the shared window, inside the hourly one.
	ageDedupKeys(f, 6*time.Minute)
	if err := f.handleLine(shellExecLine(t, `-c "echo two"`)); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth after six minutes = %d, want 1 (coalesced)", got)
	}
	if got := f.stats.coalesced.Load(); got != 1 {
		t.Fatalf("coalesced = %d, want 1", got)
	}

	// Past the hour the next shell is a new event again.
	ageDedupKeys(f, 61*time.Minute)
	if err := f.handleLine(shellExecLine(t, `-c "echo three"`)); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 2 {
		t.Fatalf("queue depth after an hour = %d, want 2", got)
	}
}

func TestShellExecCoalescesAcrossShellBinaries(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"

	alertFor := func(binary, cmd string) alert {
		t.Helper()
		a, ok := tetragonAlert(execLine(t, binary, cmd, "/usr/sbin/sshd"), cfg)
		if !ok {
			t.Fatalf("%s %q must alert", binary, cmd)
		}
		return a
	}
	if alertFor("/bin/sh", `-c "echo one"`).dedupKey() != alertFor("/bin/bash", `-c "echo two"`).dedupKey() {
		t.Fatal("sh and bash on one host must share the shell key")
	}
}

func TestTCPConnectRepeatsCoalesceAndKeepRecords(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	line := tcpLine(t, "/usr/bin/curl", "https://service/", "203.0.113.10", 443)
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	ageDedupKeys(f, 6*time.Minute)
	if err := f.handleLine(line); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth = %d, want 1 (repeat coalesced)", got)
	}
	if got := f.stats.coalesced.Load(); got != 1 {
		t.Fatalf("coalesced = %d, want 1", got)
	}

	other := tcpLine(t, "/usr/bin/curl", "https://service/", "203.0.113.11", 443)
	if err := f.handleLine(other); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 2 {
		t.Fatalf("queue depth = %d, want 2 (a new destination must page)", got)
	}

	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 3 {
		t.Fatalf("recorded %d detections, want 3", n)
	}
}

func TestTCPDestinationLabelSeparatesAddresses(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"
	alertFor := func(daddr string) alert {
		t.Helper()
		a, ok := tetragonAlert(tcpLine(t, "/usr/bin/curl", "https://service/", daddr, 443), cfg)
		if !ok {
			t.Fatalf("connect to %s must alert", daddr)
		}
		return a
	}
	one, two := alertFor("203.0.113.10"), alertFor("203.0.113.11")
	if one.Labels["dest"] == "" || one.Labels["dest"] == two.Labels["dest"] {
		t.Fatalf("dest labels must be present and distinct: %q vs %q", one.Labels["dest"], two.Labels["dest"])
	}
	if strings.Contains(one.Labels["dest"], "203.0.113.10") {
		t.Fatal("dest label must not carry the address")
	}
	if alertFor("203.0.113.10").Labels["dest"] != one.Labels["dest"] {
		t.Fatal("the same destination must keep one label")
	}
}

func TestDeletedBinaryIsStillClassified(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_exec"
	a, ok := tetragonAlert(execLine(t, "/bin/sh (deleted)", `-c "echo hi"`, "/usr/sbin/sshd"), cfg)
	if !ok {
		t.Fatal("a shell replaced in place must still be classified")
	}
	if a.Labels["binary"] != "sh" {
		t.Fatalf("binary label = %q, want sh", a.Labels["binary"])
	}
}

func TestRepeatedDetectionWithinDedupWindowIsRecorded(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig()
	cfg.events = "process_kprobe"
	cfg.detectionsFile = filepath.Join(dir, "detections.jsonl")

	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	line := tcpLine(t, "/usr/bin/curl", "https://service/", "203.0.113.20", 8443)
	for i := 0; i < 2; i++ {
		if err := f.handleLine(line); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.queue.depth(); got != 1 {
		t.Fatalf("queue depth = %d, want 1 (delivery deduplicated)", got)
	}
	if got := f.stats.deduped.Load(); got != 1 {
		t.Fatalf("deduped = %d, want 1", got)
	}
	body, err := os.ReadFile(cfg.detectionsFile)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(splitLines(string(body))); n != 2 {
		t.Fatalf("recorded %d detections, want 2 (dedup is delivery-only)", n)
	}
}
