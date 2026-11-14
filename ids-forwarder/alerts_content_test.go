package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func privilegeKprobeLine(function, args string) string {
	line, _ := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"function_name": function,
			"policy_name":   "privilege-raises",
			"process": map[string]any{
				"binary":    "/usr/sbin/squid",
				"arguments": "--kid squid-1 --foreground -sYC",
				"pid":       4242,
				"uid":       13,
			},
			"args": args,
		},
	})
	return string(line)
}

func TestPrivilegeSummaryNamesTheTransition(t *testing.T) {
	caps := `[{"cap_effective_arg":"0000000000000000"},{"cap_effective_arg":"0000000000000400"},{"cap_permitted_arg":"000001ffffffffff"},{"cap_permitted_arg":"0000000000000400"}]`
	cases := []struct {
		name     string
		function string
		args     string
		want     string
	}{
		{"setuid to root", "__sys_setuid", `[{"int_arg":0}]`, "setuid(0)"},
		{"setresuid ids", "__sys_setresuid", `[{"int_arg":-1},{"int_arg":0},{"int_arg":-1}]`, "setresuid(-1,0,-1)"},
		{"capability delta", "security_capset", caps, "caps effective 0x0->0x400"},
	}
	for _, tc := range cases {
		got := privilegeTransition(tc.function, json.RawMessage(tc.args))
		if got != tc.want {
			t.Fatalf("%s: %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestPrivilegeSummaryDropsTheRawArgumentBlob(t *testing.T) {
	caps := `[{"cap_effective_arg":"0000000000000000"},{"cap_effective_arg":"0000000000000400"}]`
	body := &tetragonLog{
		PolicyName: "privilege-raises",
		Process:    tetragonProcess{Binary: "/usr/sbin/squid", Arguments: "--foreground -z"},
		Args:       json.RawMessage(caps),
	}
	summary := eventSummaryRaw("process_kprobe", body)
	if strings.Contains(summary, "kprobe_args") || strings.Contains(summary, "cap_effective_arg") {
		t.Fatalf("summary still carries the raw bitmap dump: %q", summary)
	}
	if !strings.Contains(summary, "caps effective 0x0->0x400") {
		t.Fatalf("summary lost the transition: %q", summary)
	}
}

func TestSquidDaemonShapesAreRoutine(t *testing.T) {
	for _, args := range []string{
		"-k rotate",
		"--kid squid-1 --foreground -sYC",
		"--foreground -z",
		"-YC -f /etc/squid/squid.conf",
	} {
		body := &tetragonLog{
			PolicyName:   "privilege-raises",
			FunctionName: "__sys_setuid",
			Process:      tetragonProcess{Binary: "/usr/sbin/squid", Arguments: args},
			Args:         json.RawMessage(`[{"int_arg":0}]`),
		}
		if got := routinePrivilegeReason(body); got != "expected_privilege" {
			t.Fatalf("squid %q: reason=%q, want expected_privilege", args, got)
		}
	}
	// A bare invocation has none of the daemon's flags: a payload that is
	// not the packaged proxy must not inherit the classification.
	body := &tetragonLog{
		PolicyName:   "privilege-raises",
		FunctionName: "__sys_setuid",
		Process:      tetragonProcess{Binary: "/usr/sbin/squid", Arguments: "-f /tmp/evil.conf"},
		Args:         json.RawMessage(`[{"int_arg":0}]`),
	}
	if got := routinePrivilegeReason(body); got != "" {
		t.Fatalf("unexpected suppression for a non-daemon squid invocation: %q", got)
	}
}

func TestEnqueuedAlertCarriesADescription(t *testing.T) {
	cfg := testConfig()
	f, err := newForwarder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer f.closeState()

	a := newAlert(cfg, sourceTetragon, "warning", "host", "privilege-raises", "privilege-raises: /usr/sbin/squid setuid(0)")
	a.Labels["origin"] = "unit"
	a.Labels["actor"] = "squid"
	a.Labels["uid"] = "13"
	a.Labels["kind"] = "privilege"
	if err := f.enqueue(a); err != nil {
		t.Fatal(err)
	}
	delivered, ok := f.queue.pop()
	if !ok {
		t.Fatal("alert was not delivered")
	}
	description := delivered.Annotations["description"]
	for _, want := range []string{"host=test-host", "layer=host", "origin=unit", "actor=squid", "uid=13",
		"rule=privilege-raises", "class=escalation_nonroot", "delivery=page"} {
		if !strings.Contains(description, want) {
			t.Fatalf("description %q is missing %q", description, want)
		}
	}
}
