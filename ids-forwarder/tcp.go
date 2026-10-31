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

// internalServicePort covers the cluster's metrics and inference service
// range. New backends should not require another expected-port map entry;
// node-management ports (SSH, etcd, Ceph) stay outside this range and remain
// visible for these binaries.
func internalServicePort(port uint16) bool {
	return (port >= 8000 && port <= 9999) || port == 11434 || port == 52415
}

// containerNetwork is the Incus bridge range. A node's own sshd dialing a
// container's SSH port is the jump-host path used to reach instances; the
// same dial from another host stays visible.
var containerNetwork = netip.MustParsePrefix("10.100.0.0/24")

// containerBridges are host-local container networks: the Incus bridge and
// docker0. Traffic to them from a container stays inside the host.
var containerBridges = []netip.Prefix{
	containerNetwork,
	netip.MustParsePrefix("172.17.0.0/16"),
}

// cephPorts are the monitor, manager and OSD ports. Tetragon attributes
// kernel-side connects to [kthreadd] when no user process owns the socket.
var cephPorts = map[uint16]struct{}{
	3300: {}, 6789: {}, 6800: {}, 6801: {}, 6802: {}, 6803: {}, 6810: {},
}

// containerProbeReason covers the few in-container probes that reach a
// cluster address on purpose. The list stays explicit: a container reaching
// another node is the lateral-movement signal the IDS exists to show, so a
// broad "container traffic is fine" rule here would defeat it.
func containerProbeReason(body *tetragonLog) string {
	if body == nil || body.FunctionName != "tcp_connect" {
		return ""
	}
	base := strings.TrimSuffix(path.Base(body.Process.Binary), " (deleted)")
	// Vikunja's image ships a healthcheck subcommand (the container is
	// distroless) and it checks service.publicurl, which resolves to the
	// storage VIP. Every probe is therefore a container -> VIP connect.
	if base == "vikunja" && strings.Contains(body.Process.Arguments, "healthcheck") {
		return "expected_tcp"
	}
	return ""
}

// tcpConn is the part of a tcp_connect event the suppression rules need.
type tcpConn struct {
	binary string
	base   string
	args   string
	addr   netip.Addr
	port   uint16
}

// newTCPConn extracts the destination and process identity, or reports that
// the event is not a connect we can classify.
func newTCPConn(body *tetragonLog) (tcpConn, bool) {
	if body == nil || body.FunctionName != "tcp_connect" {
		return tcpConn{}, false
	}
	addr, port, ok := tcpDestination(body.Args)
	if !ok || port == 0 {
		return tcpConn{}, false
	}
	binary := body.Process.Binary
	return tcpConn{
		binary: binary,
		// Tetragon keeps the " (deleted)" suffix after an in-place binary
		// update; it is not part of the process identity.
		base: strings.TrimSuffix(path.Base(binary), " (deleted)"),
		args: body.Process.Arguments,
		addr: addr,
		port: port,
	}, true
}

// expectedTCPReason suppresses a known service connecting to its normal
// service port on the private cluster network. The same binary talking to an
// unexpected port, or to an external address, still produces an alert.
func expectedTCPReason(body *tetragonLog) string {
	conn, ok := newTCPConn(body)
	if !ok {
		return ""
	}
	// Fixed automation talks to fixed destinations, so these rules apply
	// regardless of address family.
	if automationDial(conn) || containerProbeReason(body) != "" {
		return "expected_tcp"
	}
	// Everything below is cluster-internal; a public destination from one
	// of these binaries stays visible.
	if !conn.addr.IsPrivate() {
		return ""
	}
	if clusterDial(conn) {
		return "expected_tcp"
	}
	return ""
}

// automationDial covers daemons and scripts whose dials are fixed: the
// blackbox exporter exists to probe external endpoints, apt's methods reach
// the mirror through the cluster proxy, and ntfy-webhook and squid are the
// notification and egress paths.
func automationDial(c tcpConn) bool {
	switch {
	case c.base == "sshd":
		// A container's sshd dials the next hop's SSH port when a client
		// uses the jump host. This is the documented access path into the
		// Incus bridge, and it only covers dials inside that bridge.
		return c.port == 22 && containerNetwork.Contains(c.addr)
	case c.base == "[kthreadd]":
		// Tetragon reports kernel-side connects without an owning process
		// as [kthreadd]; those are the Ceph OSD and messenger sockets.
		_, ok := cephPorts[c.port]
		return ok
	case c.base == "prometheus-blackbox-exporter", c.base == "squid":
		return true
	case strings.HasPrefix(c.base, "smokescreen"):
		return true
	case c.base == "ntfy-webhook.py" || strings.Contains(c.args, "ntfy-webhook.py"):
		return true
	case c.base == "frpc":
		return c.port == 7000
	case c.base == "fwupd" || c.base == "snapd":
		return c.port == 443
	case c.base == "NetworkManager":
		return c.port == 80
	case strings.Contains(c.binary, "/apt/methods/"):
		return c.port == 443 || c.port == 3128
	}
	if c.args == "" {
		return false
	}
	return automationArgs(c)
}

// automationArgs covers cluster scripts whose binary name is generic
// (python3, ycluster) and whose purpose is only visible in the command line.
func automationArgs(c tcpConn) bool {
	switch {
	case strings.Contains(c.args, "ycluster vm idle") && c.port == 443:
		// The hourly leader job posts idle-VM notices to the Zulip API.
		return true
	case strings.Contains(c.args, "/usr/local/lib/cluster/hosting-balance-collector"):
		// The hosting-balance collector's provider API is a known external
		// egress path; its output is the balance metric, not this connect.
		return true
	case strings.Contains(c.args, "/usr/local/lib/cluster/cluster-heartbeat"):
		// The leader's dead-man's-switch ping is a fixed external call;
		// match the script path rather than the destination.
		return true
	case c.port == 2381 && c.addr.IsPrivate() && strings.Contains(c.args, "/usr/local/"):
		// Cluster collectors talk to etcd on the client port.
		return true
	case isPython(c.base) && strings.Contains(c.args, "ycluster.utils.gateway_health"):
		return c.port == 2381 || c.port == 7000
	}
	return false
}

func isPython(base string) bool {
	return base == "python3" || base == "python3.12"
}

// clusterDial covers dials to cluster-internal services. Callers guarantee
// the destination is private.
func clusterDial(c tcpConn) bool {
	// Squid is the cluster's egress proxy: every client, including apt's
	// HTTP method, reaches its one private address on this port.
	if c.port == 3128 {
		return true
	}
	// Port forwarders for published container ports only ever dial the
	// container side of their own bridge.
	if c.base == "rootlessport" || c.base == "docker-proxy" {
		return true
	}
	// nginx dials its configured upstreams on the cluster network.
	if c.base == "nginx" && c.port == 8188 {
		return true
	}
	return cephDial(c) || serviceDial(c)
}

func cephDial(c tcpConn) bool {
	switch c.base {
	case "ceph":
		// The CLI reaches the monitor, manager and OSD ports.
		return c.port == 3300 || c.port == 6789 ||
			c.port == 6800 || c.port == 6801 || c.port == 6802 || c.port == 6810
	case "ceph-mgr":
		return (c.port >= 6800 && c.port <= 6802) || c.port == 6810
	case "ceph-mon":
		return c.port == 3300 || c.port == 6789 || c.port == 6810
	case "ceph-osd":
		return c.port == 6789 || c.port == 6810
	case "ceph-mds":
		return c.port == 6810
	}
	return false
}

func serviceDial(c tcpConn) bool {
	switch c.base {
	case "etcdctl":
		return c.port == 2381
	case "ycluster":
		return c.port == 2381 || c.port == 9091 || c.port == 5432
	case "etcd":
		return c.port == 2382
	case "microcephd":
		return c.port == 7443
	case "dnsmasq":
		return c.port == 53 || c.port == 853
	case "prometheus":
		return registeredPort(expectedPrometheusPorts, c.port)
	case "prometheus-alertmanager":
		return c.port == 9094
	case "ids-forwarder":
		return c.port == 9093
	case "local-ai-proxy":
		return registeredPort(expectedLocalAIProxyPorts, c.port)
	}
	if !isPython(c.base) {
		return false
	}
	return adminPythonDial(c)
}

// registeredPort reports whether port is a known service port or part of
// the cluster's metrics and inference range.
func registeredPort(known map[uint16]struct{}, port uint16) bool {
	if _, ok := known[port]; ok {
		return true
	}
	return internalServicePort(port)
}

// adminPythonDial covers the admin service and its target updater, which
// share the python3 binary.
func adminPythonDial(c tcpConn) bool {
	if c.port == 2381 && strings.Contains(c.args, "update-blackbox-targets") {
		return true
	}
	if !strings.Contains(c.args, "/opt/admin-services/app.py") {
		return false
	}
	_, ok := expectedAdminServicePorts[c.port]
	return ok || internalServicePort(c.port)
}

// containerEgressReason suppresses ordinary internet egress from a user
// container. Containers have no route into the cluster, so a connect to a
// private address is not ordinary egress: it means the network policy was
// bypassed or misconfigured, and it stays alertable.
func containerEgressReason(body *tetragonLog, layer string) string {
	if layer != "container" || body == nil || body.FunctionName != "tcp_connect" {
		return ""
	}
	addr, _, ok := tcpDestination(body.Args)
	if !ok {
		return ""
	}
	if addr.IsLoopback() || addr.IsUnspecified() || addr.IsLinkLocalUnicast() {
		return "container_bridge"
	}
	if !addr.IsPrivate() {
		return "tenant_egress"
	}
	// The container bridge is host-local networking between instances the
	// network ACL already governs; it is not a path into the cluster.
	for _, prefix := range containerBridges {
		if prefix.Contains(addr) {
			return "container_bridge"
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
