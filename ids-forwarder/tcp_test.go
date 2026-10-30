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
		{"deepseek moE backend", "/usr/local/bin/local-ai-proxy", "serve", "10.0.0.112", 9219},
		{"prometheus new inference port", "/usr/bin/prometheus", "", "10.0.0.112", 9219},
		{"admin service new inference port", "/usr/bin/python3.12", "/opt/admin-services/app.py", "10.0.0.112", 9219},
		{"gateway health", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "10.0.0.11", 2381},
		{"gateway health public endpoint", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "203.0.113.10", 7000},
		{"admin service", "/usr/bin/python3.12", "/opt/admin-services/app.py", "10.0.0.12", 8000},
		{"dnsmasq resolver", "/usr/sbin/dnsmasq", "-x /run/dnsmasq/dnsmasq.pid", "10.0.0.11", 53},
		{"hosting balance provider", "/usr/bin/python3", "/usr/local/lib/cluster/hosting-balance-collector", "203.0.113.110", 443},
		{"health api poll", "/usr/bin/python3.12", "/opt/admin-services/app.py", "10.0.0.11", 12724},
		{"squid egress proxy", "/usr/lib/apt/methods/http", "", "10.0.0.254", 3128},
		{"apt mirror over https", "/usr/lib/apt/methods/https", "", "203.0.113.20", 443},
		{"bastion jump to container", "/usr/sbin/sshd", "-D -R", "10.100.0.13", 22},
		{"kernel-attributed ceph osd", "[kthreadd]", "", "10.0.0.11", 6803},
		{"blackbox external probe", "/usr/bin/prometheus-blackbox-exporter", "--config.file=/etc/prometheus/blackbox.yml", "203.0.113.30", 443},
		{"ntfy notifier", "/usr/bin/python3.12", "/usr/local/bin/ntfy-webhook.py", "203.0.113.40", 443},
		{"squid egress", "/usr/sbin/squid", "--kid squid-1", "203.0.113.50", 443},
		{"networkmanager connectivity check", "/usr/sbin/NetworkManager", "--no-daemon", "203.0.113.60", 80},
		{"vikunja healthcheck", "/app/vikunja/vikunja", "healthcheck", "203.0.113.70", 443},
		{"cluster collector to etcd", "/usr/bin/python3", "/usr/local/bin/collect-model-stats", "10.0.0.11", 2381},
		{"cluster heartbeat ping", "/usr/bin/python3", "/usr/local/lib/cluster/cluster-heartbeat", "203.0.113.80", 443},
		{"podman port forwarder", "/usr/libexec/podman/rootlessport", "", "10.88.0.4", 9000},
		{"ycluster cli", "/usr/local/bin/ycluster", "wg reconcile --up", "10.0.0.13", 2381},
		{"ycluster vm idle notify", "/usr/local/bin/ycluster", "/usr/local/bin/ycluster vm idle --notify", "10.0.0.12", 9091},
		{"ycluster vm idle database", "/usr/local/bin/ycluster", "/usr/local/bin/ycluster vm idle --notify", "10.0.0.12", 5432},
		{"deleted podman port forwarder", "/usr/libexec/podman/rootlessport (deleted)", "", "10.88.0.4", 9000},
		{"frp tunnel client", "/usr/local/bin/frpc", "-c /etc/frp/ssh-client.toml", "10.0.0.12", 7000},
		{"frp tunnel client to public server", "/usr/local/bin/frpc", "-c /etc/frp/client.toml", "203.0.113.93", 7000},
		{"smokescreen egress proxy", "/usr/local/bin/smokescreen-abc123-go-1.26.0", "--listen-ip 127.0.0.1", "203.0.113.90", 443},
		{"nginx upstream", "/usr/sbin/nginx", "", "10.0.0.12", 8188},
		{"fwupd firmware check", "/usr/libexec/fwupd/fwupd", "", "203.0.113.91", 443},
		{"snapd store check", "/snap/snapd/28254/usr/lib/snapd/snapd", "", "203.0.113.92", 443},
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
		{"prometheus to etcd", "/usr/bin/prometheus", "", "10.0.0.11", 2381},
		{"unknown binary expected service port", "/usr/bin/curl", "https://10.0.0.11:2381/", "10.0.0.11", 2381},
		{"unknown python script", "/usr/bin/python3", "-m custom_script", "10.0.0.11", 2381},
		{"gateway health unexpected port", "/usr/bin/python3", "-m ycluster.utils.gateway_health enp87s0", "203.0.113.10", 7001},
		{"local ai external destination", "/usr/local/bin/local-ai-proxy", "serve", "203.0.113.10", 9218},
		{"sshd to container on unusual port", "/usr/sbin/sshd", "-D -R", "10.100.0.13", 2222},
		{"sshd to cluster host ssh", "/usr/sbin/sshd", "", "10.0.0.12", 22},
		{"kthreadd to non-ceph port", "[kthreadd]", "", "10.0.0.11", 22},
		{"unknown python to etcd port", "/usr/bin/python3", "-m mystery", "10.0.0.11", 2381},
		{"other process to heartbeat destination", "/usr/bin/python3", "-m mystery", "203.0.113.80", 443},
		{"unexpected proxy port on external host", "/usr/bin/curl", "https://203.0.113.10:3128/", "203.0.113.10", 3128},
		{"apt method on unusual port", "/usr/lib/apt/methods/http", "", "10.0.0.11", 8443},
		{"nginx on unexpected port", "/usr/sbin/nginx", "", "10.0.0.11", 8189},
		{"frp client on unexpected port", "/usr/local/bin/frpc", "-c /etc/frp/ssh-client.toml", "10.0.0.11", 7001},
		{"frp client to public server on unexpected port", "/usr/local/bin/frpc", "-c /etc/frp/client.toml", "203.0.113.93", 7001},
		{"fwupd on unexpected port", "/usr/libexec/fwupd/fwupd", "", "203.0.113.91", 80},
		{"ycluster notify to external host", "/usr/local/bin/ycluster", "/usr/local/bin/ycluster vm idle --notify", "203.0.113.10", 9091},
		{"other process to notify port", "/usr/bin/curl", "", "10.0.0.12", 9091},
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
