package main

import (
	"testing"
	"time"
)

func TestBootSuppressionIsNarrow(t *testing.T) {
	boot := time.Date(2026, 11, 11, 15, 1, 0, 0, time.UTC)
	f := &forwarder{
		cfg:      config{bootGrace: 5 * time.Minute},
		bootTime: boot,
	}
	at := func(offset time.Duration, rule, origin, binary string) alert {
		return alert{
			Labels:    map[string]string{"rule_id": rule, "origin": origin, "binary": binary},
			eventTime: boot.Add(offset),
		}
	}

	cases := []struct {
		name  string
		alert alert
		want  bool
	}{
		{"container api at boot", at(time.Minute, "container-api", "unit", "incus"), true},
		{"unit process at boot", at(time.Minute, "process_exec", "unit", "bash"), true},
		{"unknown process at boot", at(time.Minute, "process_exec", "unknown", "bash"), true},
		{"privilege at boot", at(time.Minute, "privilege-raises", "unit", ""), true},
		{"module at boot", at(time.Minute, "kernel-modules", "unknown", "modprobe"), true},
		{"interactive process at boot", at(time.Minute, "process_exec", "interactive", "bash"), false},
		{"private lateral at boot", at(time.Minute, "tcp-connect", "unit", "sshd"), false},
		{"file write at boot", at(time.Minute, "exec-and-sensitive-file-writes", "unit", ""), false},
		{"forwarder gap at boot", at(time.Minute, "forwarder-gap", "unknown", ""), false},
		{"after grace", at(6*time.Minute, "process_exec", "unit", "bash"), false},
		{"before boot", at(-time.Minute, "process_exec", "unit", "bash"), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := f.bootSuppress(tc.alert); got != tc.want {
				t.Fatalf("bootSuppress = %v, want %v", got, tc.want)
			}
		})
	}

	f.cfg.bootGrace = 0
	if f.bootSuppress(at(time.Minute, "process_exec", "unit", "bash")) {
		t.Fatal("boot suppression must be disabled when the grace is zero")
	}
}

func TestBootSuppressionSkipsDelivery(t *testing.T) {
	f := &forwarder{
		cfg:      config{bootGrace: 5 * time.Minute},
		bootTime: time.Now().Add(-time.Minute),
		queue:    alertQueue{max: 1},
		dedup:    newDedup(time.Hour),
	}
	a := alert{
		Labels:    map[string]string{"rule_id": "process_exec", "origin": "unit", "binary": "bash"},
		eventTime: time.Now(),
	}
	if err := f.enqueue(a); err != nil {
		t.Fatal(err)
	}
	if got := f.queue.depth(); got != 0 {
		t.Fatalf("queue depth = %d, want 0", got)
	}
	if got := f.stats.bootSuppressed.Load(); got != 1 {
		t.Fatalf("boot suppressed = %d, want 1", got)
	}
}
