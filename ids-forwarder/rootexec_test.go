package main

import (
	"fmt"
	"testing"
)

func TestRootExecUserPath(t *testing.T) {
	tests := []struct {
		path string
		want bool
	}{
		{"/home/ubuntu/payload", true},
		{"/var/tmp/build/run", true},
		{"/tmp/payload", false},
		{"/dev/shm/payload", false},
		{"/usr/bin/true", false},
		{"/opt/src/tool", false},
		{"/root/go/bin/tool", false},
		{"/home", false},
	}
	for _, tt := range tests {
		if got := rootExecUserPath(tt.path); got != tt.want {
			t.Errorf("rootExecUserPath(%q) = %v, want %v", tt.path, got, tt.want)
		}
	}
}

func rootExecLine(uid int, image string, pod bool) string {
	podField := ""
	if pod {
		podField = `,"pod":{"namespace":"default","name":"tenant"}`
	}
	return fmt.Sprintf(`{"time":"2026-10-31T12:00:00Z","process_exec":{"process":{"pid":424242,"uid":%d,"binary":%q,"arguments":"--run"%s},"parent":{"pid":1,"binary":"/bin/bash"}}}`, uid, image, podField)
}

func rootExecConfig(enabled bool) config {
	cfg := defaultConfig()
	cfg.hostname = "test"
	cfg.events = "process_exec"
	cfg.execBinaries = ""
	cfg.execRootUserPath = enabled
	cfg.logf = func(string, ...any) {}
	return cfg
}

func TestRootExecUserPathAlert(t *testing.T) {
	cases := []struct {
		name string
		line string
		cfg  config
		want bool
	}{
		{"root from home", rootExecLine(0, "/home/ubuntu/tool", false), rootExecConfig(true), true},
		{"root from var tmp", rootExecLine(0, "/var/tmp/tool", false), rootExecConfig(true), true},
		{"non-root from home", rootExecLine(1000, "/home/ubuntu/tool", false), rootExecConfig(true), false},
		{"root from system path", rootExecLine(0, "/usr/bin/tool", false), rootExecConfig(true), false},
		{"root from tmp covered upstream", rootExecLine(0, "/tmp/tool", false), rootExecConfig(true), false},
		{"feature disabled", rootExecLine(0, "/home/ubuntu/tool", false), rootExecConfig(false), false},
		{"container root", rootExecLine(0, "/home/ubuntu/tool", true), rootExecConfig(true), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, _, ok, _ := tetragonAlertDecision(tc.line, tc.cfg)
			if ok != tc.want {
				t.Fatalf("ok = %v, want %v", ok, tc.want)
			}
			if !tc.want {
				return
			}
			if got := a.Labels["rule_id"]; got != "exec-root-user-path" {
				t.Errorf("rule_id = %q", got)
			}
			if a.Labels["layer"] != "host" {
				t.Errorf("layer = %q", a.Labels["layer"])
			}
			if a.Labels["binary"] != "tool" {
				t.Errorf("binary = %q", a.Labels["binary"])
			}
		})
	}
}

func TestRootExecUserPathDedupsPerPath(t *testing.T) {
	cfg := rootExecConfig(true)
	a, _, ok, _ := tetragonAlertDecision(rootExecLine(0, "/home/ubuntu/tool", false), cfg)
	if !ok {
		t.Fatal("expected alert")
	}
	b, _, ok, _ := tetragonAlertDecision(rootExecLine(0, "/home/ubuntu/tool", false), cfg)
	if !ok {
		t.Fatal("expected alert")
	}
	c, _, ok, _ := tetragonAlertDecision(rootExecLine(0, "/home/ubuntu/other", false), cfg)
	if !ok {
		t.Fatal("expected alert")
	}
	if a.dedupKey() != b.dedupKey() {
		t.Error("same binary must dedup regardless of arguments")
	}
	if a.dedupKey() == c.dedupKey() {
		t.Error("different binaries must not dedup")
	}
}
