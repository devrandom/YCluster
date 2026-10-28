package main

import (
	"encoding/json"
	"net/netip"
	"path"
	"strings"
)

// tetragonSockArg is the subset of a tcp_connect kprobe argument needed to
// classify expected cluster traffic.
type tetragonSockArg struct {
	SockArg *tetragonSocket `json:"sock_arg"`
}

type tetragonSocket struct {
	DAddr string `json:"daddr"`
	DPort uint16 `json:"dport"`
}

var expectedPrometheusPorts = map[uint16]struct{}{
	8067: {}, 9090: {}, 9093: {}, 9094: {}, 9095: {},
	9100: {}, 9101: {}, 9115: {}, 9187: {},
	9216: {}, 9217: {}, 9218: {}, 9222: {},
	9283: {}, 9400: {}, 9443: {},
}

var expectedLocalAIProxyPorts = map[uint16]struct{}{
	8000: {}, 8001: {}, 8080: {}, 8081: {}, 9000: {},
	9216: {}, 9217: {}, 9218: {}, 11434: {}, 52415: {},
}

var expectedAdminServicePorts = map[uint16]struct{}{
	8000: {}, 8080: {}, 9000: {},
	9216: {}, 9217: {}, 9218: {}, 12724: {}, 52415: {},
}

// containerNetwork is the Incus bridge range. A node's own sshd dialing a
// container's SSH port is the jump-host path used to reach instances; the
// same dial from another host stays visible.
var containerNetwork = netip.MustParsePrefix("10.100.0.0/24")

// cephPorts are the monitor, manager and OSD ports. Tetragon attributes
// kernel-side connects to [kthreadd] when no user process owns the socket.
var cephPorts = map[uint16]struct{}{
	3300: {}, 6789: {}, 6800: {}, 6801: {}, 6802: {}, 6803: {}, 6810: {},
}

// expectedTCPReason suppresses a known service connecting to its normal
// service port on the private cluster network. The same binary talking to an
// unexpected port, or to an external address, still produces an alert.
func expectedTCPReason(body *tetragonLog) string {
	if body == nil || body.FunctionName != "tcp_connect" {
		return ""
	}
	addr, port, ok := tcpDestination(body.Args)
	if !ok || port == 0 {
		return ""
	}

	base := path.Base(body.Process.Binary)
	args := body.Process.Arguments

	// A container's sshd dials the next hop's SSH port when a client uses
	// the jump host. This is the documented access path into the Incus
	// bridge, and it only covers dials inside that bridge.
	if base == "sshd" && port == 22 && containerNetwork.Contains(addr) {
		return "expected_tcp"
	}
	// Tetragon reports kernel-side connects without an owning process as
	// [kthreadd]; those are the Ceph OSD and messenger sockets.
	if base == "[kthreadd]" {
		if _, ok := cephPorts[port]; ok {
			return "expected_tcp"
		}
	}
	// The blackbox exporter exists to probe external endpoints; apt's
	// methods reach the mirror through the cluster proxy; ntfy-webhook and
	// squid are the notification and egress paths.
	switch {
	case base == "prometheus-blackbox-exporter":
		return "expected_tcp"
	case base == "ntfy-webhook.py" || strings.Contains(args, "ntfy-webhook.py"):
		return "expected_tcp"
	case base == "squid":
		return "expected_tcp"
	case strings.Contains(body.Process.Binary, "/apt/methods/") && (port == 443 || port == 3128):
		return "expected_tcp"
	case base == "NetworkManager" && port == 80:
		return "expected_tcp"
	case base == "vikunja" && strings.Contains(args, "healthcheck"):
		return "expected_tcp"
	}
	// Cluster collectors talk to etcd on the client port.
	if port == 2381 && addr.IsPrivate() && strings.Contains(args, "/usr/local/") {
		return "expected_tcp"
	}
	// The leader's dead-man's-switch ping is a fixed external call; its
	// failure is an unanswered check on the far side, not a missing event
	// here. Match the script path rather than the destination so another
	// process reaching that host still alerts. A repointed URL is not
	// visible here by design; the write to the environment file stays
	// alertable and is the signal for that change.
	if strings.Contains(args, "/usr/local/lib/cluster/cluster-heartbeat") {
		return "expected_tcp"
	}
	if (base == "python3" || base == "python3.12") &&
		strings.Contains(args, "ycluster.utils.gateway_health") &&
		(port == 2381 || port == 7000) {
		return "expected_tcp"
	}
	if !addr.IsPrivate() {
		return ""
	}
	// Squid is the cluster's egress proxy: every client, including apt's
	// HTTP method, reaches its one private address on this port. The proxy
	// logs the real destination, so the connect itself carries no detection
	// value.
	if port == 3128 {
		return "expected_tcp"
	}
	// Port forwarders for published container ports only ever dial the
	// container side of their own bridge.
	if base == "rootlessport" || base == "docker-proxy" {
		return "expected_tcp"
	}
	switch base {
	case "etcdctl", "ycluster":
		if port == 2381 {
			return "expected_tcp"
		}
	case "etcd":
		if port == 2382 {
			return "expected_tcp"
		}
	case "ceph":
		switch port {
		case 3300, 6789, 6800, 6801, 6802, 6810:
			return "expected_tcp"
		}
	case "ceph-mgr":
		if (port >= 6800 && port <= 6802) || port == 6810 {
			return "expected_tcp"
		}
	case "ceph-mon":
		switch port {
		case 3300, 6789, 6810:
			return "expected_tcp"
		}
	case "ceph-osd":
		switch port {
		case 6789, 6810:
			return "expected_tcp"
		}
	case "ceph-mds":
		if port == 6810 {
			return "expected_tcp"
		}
	case "microcephd":
		if port == 7443 {
			return "expected_tcp"
		}
	case "prometheus":
		if _, ok := expectedPrometheusPorts[port]; ok {
			return "expected_tcp"
		}
	case "prometheus-alertmanager":
		if port == 9094 {
			return "expected_tcp"
		}
	case "ids-forwarder":
		if port == 9093 {
			return "expected_tcp"
		}
	case "local-ai-proxy":
		if _, ok := expectedLocalAIProxyPorts[port]; ok {
			return "expected_tcp"
		}
	case "python3", "python3.12":
		if port == 2381 && strings.Contains(args, "update-blackbox-targets") {
			return "expected_tcp"
		}
		if strings.Contains(args, "/opt/admin-services/app.py") {
			if _, ok := expectedAdminServicePorts[port]; ok {
				return "expected_tcp"
			}
		}
	}
	return ""
}

func tcpDestination(raw json.RawMessage) (netip.Addr, uint16, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return netip.Addr{}, 0, false
	}
	var args []tetragonSockArg
	if err := json.Unmarshal(raw, &args); err != nil {
		return netip.Addr{}, 0, false
	}
	for _, arg := range args {
		if arg.SockArg == nil {
			continue
		}
		addr, err := netip.ParseAddr(arg.SockArg.DAddr)
		if err != nil {
			continue
		}
		return addr.Unmap(), arg.SockArg.DPort, true
	}
	return netip.Addr{}, 0, false
}
