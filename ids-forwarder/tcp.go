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
	SAddr string `json:"saddr"`
	DAddr string `json:"daddr"`
	DPort uint16 `json:"dport"`
}

// expectedPrometheusPorts lists the scrape targets outside the
// 8000-9999 service range; in-range targets are covered by
// internalServicePort. Prometheus scrapes the admin API on 12723 for
// http_sd and the inference proxy's dedicated metrics listener on 4010.
var expectedPrometheusPorts = map[uint16]struct{}{
	4010: {}, 8067: {}, 9090: {}, 9093: {}, 9094: {}, 9095: {},
	9100: {}, 9101: {}, 9115: {}, 9187: {},
	9216: {}, 9217: {}, 9218: {}, 9222: {},
	9283: {}, 9400: {}, 9443: {}, 12723: {},
}

// expectedLocalAIProxyPorts is both the proxy's upstream set and the set of
// inference backend ports the latency probe polls.
var expectedLocalAIProxyPorts = map[uint16]struct{}{
	8000: {}, 8001: {}, 8080: {}, 8081: {}, 9000: {},
	9216: {}, 9217: {}, 9218: {}, 9219: {}, 11434: {}, 52415: {},
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
// the RFC 1918 range Docker allocates user-defined bridges from. A compose
// project gets its own network (172.26.x here), not docker0, and reaching the
// host over that bridge is still host-local: it is the same path the gateway
// nginx accepts with `allow 172.16.0.0/12`, not a route into the cluster.
var containerBridges = []netip.Prefix{
	containerNetwork,
	netip.MustParsePrefix("172.16.0.0/12"),
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
	binary := strings.TrimSuffix(body.Process.Binary, " (deleted)")
	_, port, ok := tcpDestination(body.Args)
	if !ok {
		return ""
	}
	// Vikunja's image ships a healthcheck subcommand (the container is
	// distroless) and it checks service.publicurl, which resolves to the
	// storage VIP. Every probe is therefore a container -> VIP connect on
	// the publicurl port; the exact image path and port keep a lookalike
	// binary from inheriting this without the Vikunja image.
	if binary == "/app/vikunja/vikunja" &&
		strings.Contains(body.Process.Arguments, "healthcheck") && port == 443 {
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
	// of these binaries stays visible. Loopback counts as local: a service
	// reaching its own host is neither egress nor lateral movement.
	if !conn.addr.IsPrivate() && !conn.addr.IsLoopback() {
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
		// as [kthreadd]; those are the Ceph OSD and messenger sockets, plus
		// the kernel NFS client mounting VM storage from the cluster.
		if _, ok := cephPorts[c.port]; ok {
			return true
		}
		return c.port == 2049 && c.addr.IsPrivate()
	case c.base == "prometheus-blackbox-exporter":
		// The whole purpose of this daemon is probing arbitrary endpoints.
		return true
	case c.base == "squid" && c.binary == "/usr/sbin/squid":
		// The proxy dials origins and peers. A payload named "squid" does not
		// inherit that exemption.
		return c.port == 80 || c.port == 443 || c.port == 3128
	case strings.HasPrefix(c.binary, "/usr/local/bin/smokescreen-"):
		// Versioned build path; an attacker staging a binary elsewhere does
		// not inherit the exemption.
		return c.port == 443
	case strings.HasPrefix(c.binary, "/usr/local/bin/ntfy-webhook.py") ||
		strings.HasPrefix(c.args, "/usr/local/bin/ntfy-webhook.py"):
		// Pinned install path and the notify-only HTTPS API, plus the cluster
		// etcd it uses to claim the shared budget; an argument mentioning the
		// name does not make an arbitrary payload trusted.
		return c.port == 443 || (c.port == 2381 && c.addr.IsPrivate())
	case c.base == "frpc":
		return c.port == 7000
	case c.base == "fwupd" || c.base == "snapd":
		return c.port == 443
	case c.base == "NetworkManager":
		return c.port == 80
	case strings.HasPrefix(c.binary, "/usr/lib/apt/methods/"):
		return c.port == 443 || c.port == 3128
	}
	if c.args == "" {
		return false
	}
	return automationArgs(c)
}

// automationArgs covers cluster scripts whose binary name is generic
// (python3, ycluster) and whose purpose is only visible in the command line.
// Every match is anchored to the program or its interpreter operand: argv is
// attacker-controlled, so a payload must not inherit an exemption by naming
// a cluster script in a later argument.
func automationArgs(c tcpConn) bool {
	switch {
	case c.port == 443 && yclusterIdleNotify(c.args):
		// The hourly leader job posts idle-VM notices to the Zulip API.
		return true
	case invokesScript(c.args, "/usr/local/lib/cluster/hosting-balance-collector"):
		// The hosting-balance collector's provider API is a known external
		// egress path; its output is the balance metric, not this connect.
		return c.port == 443
	case invokesScript(c.args, "/usr/local/lib/cluster/cluster-heartbeat"):
		// The leader's dead-man's-switch ping is a fixed external call, and
		// the same job records leadership in etcd.
		return c.port == 443 || (c.port == 2381 && c.addr.IsPrivate())
	case invokesScript(c.args, "/usr/local/lib/cluster/inference-interactive-probe"):
		// The latency probe polls the registered inference backends and its
		// own Prometheus. Backends come from the cluster-owned registry, but
		// keep the exemption to private inference ports so a redirected
		// target still shows up.
		_, known := expectedLocalAIProxyPorts[c.port]
		return (known && c.addr.IsPrivate()) || (c.port == 9090 && c.addr.IsLoopback())
	case c.port == 2381 && c.addr.IsPrivate() && clusterScript(c.args):
		// Cluster collectors talk to etcd on the client port.
		return true
	case c.port == 443 && isPython(c.base) && moduleRun(c.args, "osv_feed"):
		// The OSV mirror fetches advisories over HTTPS. Anchor the exemption
		// to the module invocation, not to the text appearing in an argument.
		return true
	case isPython(c.base) && strings.HasPrefix(c.args, "-m ycluster.utils.gateway_health"):
		// Module invocation only: the text in another argument is not the
		// gateway-health probe.
		return c.port == 2381 || c.port == 7000
	}
	return false
}

// invokesScript reports whether the command line launches script. Tetragon
// records the process arguments without the interpreter, so a script normally
// appears at argv[0] and only an interpreter flag can precede it.
func invokesScript(args, script string) bool {
	fields := strings.Fields(args)
	if i := scriptOperand(fields); i >= 0 && i < len(fields) {
		return fields[i] == script
	}
	return false
}

// clusterScript reports whether the command line runs a cluster-installed
// program, which is the part of argv a payload cannot forge without write
// access to /usr/local.
func clusterScript(args string) bool {
	fields := strings.Fields(args)
	i := scriptOperand(fields)
	if i < 0 || i >= len(fields) {
		return false
	}
	return strings.HasPrefix(fields[i], "/usr/local/lib/cluster/") ||
		strings.HasPrefix(fields[i], "/usr/local/bin/")
}

// scriptOperand returns the index of the program or interpreter script
// operand, or -1 for an empty command line. Accepting a single leading flag
// covers `python3 -u /usr/local/...` without letting a script's first
// argument claim the identity of the program.
func scriptOperand(fields []string) int {
	switch {
	case len(fields) == 0:
		return -1
	case len(fields) > 1 && strings.HasPrefix(fields[0], "-"):
		return 1
	}
	return 0
}

// yclusterIdleNotify matches the idle-VM notice subcommand.
func yclusterIdleNotify(args string) bool {
	fields := strings.Fields(args)
	i := scriptOperand(fields)
	if i < 0 || i+2 >= len(fields) || fields[i] != "/usr/local/bin/ycluster" {
		return false
	}
	return fields[i+1] == "vm" && fields[i+2] == "idle"
}

func isPython(base string) bool {
	return base == "python3" || base == "python3.12"
}

// moduleRun reports whether the interpreter runs module via `-m`. Tetragon
// records the arguments without the interpreter binary.
func moduleRun(args, module string) bool {
	fields := strings.Fields(args)
	return len(fields) >= 2 && fields[0] == "-m" && fields[1] == module
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
		// 8067 is the DHCP health endpoint that `cluster status` probes.
		return c.port == 2381 || c.port == 9091 || c.port == 5432 || c.port == 8067
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
		// The proxy also reads its model registry from the etcd client port.
		return registeredPort(expectedLocalAIProxyPorts, c.port) ||
			(c.port == 2381 && c.addr.IsPrivate())
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
	if c.port == 2381 && c.addr.IsPrivate() &&
		strings.HasPrefix(c.args, "/opt/admin-services/") {
		// The admin services (app.py, health-api.py) read their
		// configuration from etcd.
		return true
	}
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
	sock, ok := tcpSocket(body.Args)
	if !ok {
		return ""
	}
	addr, err := netip.ParseAddr(sock.DAddr)
	if err != nil {
		return ""
	}
	addr = addr.Unmap()
	dport := sock.DPort
	src, err := netip.ParseAddr(sock.SAddr)
	if err != nil {
		src = netip.Addr{}
	}
	if addr.IsLoopback() || addr.IsUnspecified() || addr.IsLinkLocalUnicast() {
		return "container_bridge"
	}
	if !addr.IsPrivate() {
		return "tenant_egress"
	}
	// The container bridge is host-local networking between instances the
	// network ACL already governs; it is not a path into the cluster.
	// The bridge gateway is the host, so what it exposes there is worth
	// seeing: only the inference gateway and the shared database are
	// routine, and any other port on that address stays visible.
	for _, prefix := range containerBridges {
		if prefix.Contains(addr) {
			if isBridgeGateway(src, addr) && !gatewayServicePort(dport) {
				return ""
			}
			return "container_bridge"
		}
	}
	return ""
}

// bridgeGatewayPorts are the host services a container reaches over its own
// docker bridge gateway: the inference/catch-all nginx on 80 and the shared
// PostgreSQL on 5432. Anything else on the gateway (node-exporter,
// Alertmanager, the proxy, sshd) is a connection into the host and alerts.
var bridgeGatewayPorts = map[uint16]struct{}{
	80:   {},
	5432: {},
}

func gatewayServicePort(port uint16) bool {
	_, ok := bridgeGatewayPorts[port]
	return ok
}

// isBridgeGateway reports whether dst is the default gateway of src's /24:
// Docker gives the first address of the network to the host, so for a
// container at 172.26.0.3 that is 172.26.0.1. Peers on the same bridge are
// .2 and up and stay classified as container_bridge.
func isBridgeGateway(src, dst netip.Addr) bool {
	if !src.IsValid() || !dst.IsValid() || src.Is4() != dst.Is4() {
		return false
	}
	prefix, err := src.Prefix(24)
	if err != nil {
		return false
	}
	base := prefix.Addr().As4()
	base[3] = 1
	return dst == netip.AddrFrom4(base)
}

func tcpDestination(raw json.RawMessage) (netip.Addr, uint16, bool) {
	sock, ok := tcpSocket(raw)
	if !ok {
		return netip.Addr{}, 0, false
	}
	addr, err := netip.ParseAddr(sock.DAddr)
	if err != nil {
		return netip.Addr{}, 0, false
	}
	return addr.Unmap(), sock.DPort, true
}

// tcpSocket returns the socket tuple Tetragon recorded for the connect. The
// source address matters for telling a container's bridge peer from the
// bridge gateway, which is the host.
func tcpSocket(raw json.RawMessage) (*tetragonSocket, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, false
	}
	var args []tetragonSockArg
	if err := json.Unmarshal(raw, &args); err != nil {
		return nil, false
	}
	for _, arg := range args {
		if arg.SockArg == nil {
			continue
		}
		return arg.SockArg, true
	}
	return nil, false
}
