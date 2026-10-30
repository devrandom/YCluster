package main

import (
	"encoding/json"
	"os"
	"path/filepath"
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
