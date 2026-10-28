package main

import (
	"encoding/json"
	"testing"
)

func TestExpectedTCPIsSuppressed(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct {
		name   string
		binary string
		args   string
		daddr  string
		port   int
	}{
		{"etcd client", "/usr/bin/etcdctl", "get /cluster/leader/app", "10.0.0.11", 2381},
		{"etcd peer", "/usr/bin/etcd", "", "10.0.0.12", 2382},
		{"ceph mon", "/usr/bin/ceph", "health", "10.0.0.12", 3300},
		{"ceph mgr", "/snap/microceph/1735/bin/ceph-mgr", "", "10.0.0.11", 6800},
		{"ceph mgr messenger", "/snap/microceph/1735/bin/ceph-mgr", "", "10.0.0.11", 6810},
		{"ceph mon", "/snap/microceph/1735/bin/ceph-mon", "", "10.0.0.11", 6789},
		{"ceph osd", "/snap/microceph/1735/bin/ceph-osd", "", "10.0.0.11", 6810},
		{"prometheus scrape", "/usr/bin/prometheus", "", "10.0.0.12", 9283},
		{"alertmanager peer", "/usr/bin/prometheus-alertmanager", "", "10.0.0.14", 9094},
		{"forwarder delivery", "/usr/local/bin/ids-forwarder", "", "10.0.0.14", 9093},
		{"local ai upstream", "/usr/local/bin/local-ai-proxy", "serve", "10.0.0.12", 9218},
		{"gateway health", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "10.0.0.11", 2381},
		{"gateway health public endpoint", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "203.0.113.10", 7000},
		{"admin service", "/usr/bin/python3.12", "/opt/admin-services/app.py", "10.0.0.12", 8000},
		{"squid egress proxy", "/usr/lib/apt/methods/http", "", "10.0.0.254", 3128},
		{"ycluster cli", "/usr/local/bin/ycluster", "wg reconcile --up", "10.0.0.13", 2381},
		{"ipv6 cluster", "/usr/bin/etcdctl", "get /cluster/leader/app", "fd00::11", 2381},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := tcpLine(t, tc.binary, tc.args, tc.daddr, tc.port)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if ok {
				t.Fatal("expected routine TCP event to be suppressed")
			}
			if reason != "expected_tcp" {
				t.Fatalf("reason = %q, want expected_tcp", reason)
			}
		})
	}
}

func TestUnexpectedTCPIsForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	cases := []struct {
		name   string
		binary string
		args   string
		daddr  string
		port   int
	}{
		{"etcd client unusual port", "/usr/bin/etcdctl", "get /", "10.0.0.11", 443},
		{"etcd client external destination", "/usr/bin/etcdctl", "get /", "203.0.113.10", 2381},
		{"prometheus unusual internal port", "/usr/bin/prometheus", "", "10.0.0.11", 443},
		{"unknown binary expected service port", "/usr/bin/curl", "https://10.0.0.11:2381/", "10.0.0.11", 2381},
		{"unknown python script", "/usr/bin/python3", "-m custom_script", "10.0.0.11", 2381},
		{"gateway health unexpected port", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "203.0.113.10", 7001},
		{"local ai external destination", "/usr/local/bin/local-ai-proxy", "serve", "203.0.113.10", 9218},
		{"proxy port on external host", "/usr/lib/apt/methods/http", "", "203.0.113.10", 3128},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			line := tcpLine(t, tc.binary, tc.args, tc.daddr, tc.port)
			_, _, ok, reason := tetragonAlertDecision(line, cfg)
			if !ok {
				t.Fatalf("unexpected TCP event was suppressed as %q", reason)
			}
		})
	}
}

func TestMalformedTCPArgsAreForwarded(t *testing.T) {
	cfg := testConfig()
	cfg.events = "process_kprobe"

	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"function_name": "tcp_connect",
			"process":       map[string]any{"binary": "/usr/bin/etcdctl"},
			"args":          []any{map[string]any{"sock_arg": map[string]any{"daddr": "not-an-address", "dport": 2381}}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, ok, reason := tetragonAlertDecision(string(line), cfg); !ok {
		t.Fatalf("malformed TCP event was suppressed as %q", reason)
	}
}

func tcpLine(t *testing.T, binary, args, daddr string, port int) string {
	t.Helper()
	line, err := json.Marshal(map[string]any{
		"process_kprobe": map[string]any{
			"function_name": "tcp_connect",
			"process":       map[string]any{"binary": binary, "arguments": args},
			"args": []any{
				map[string]any{
					"sock_arg": map[string]any{
						"daddr": daddr,
						"dport": port,
					},
				},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}
