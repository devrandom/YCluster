#!/usr/bin/env python3
"""Health logic for the keepalived VI_GATEWAY (gateway-VIP) track script.

Exit 0 means this node may hold the gateway VIP (its uplink can carry cluster
traffic out); exit 1 means it must not. Invoked by the thin wrapper at
/usr/local/lib/cluster/check-gateway-health, which supplies the etcd client
env and the uplink interface name as argv[1].

Fitness is judged by reachability to the frontend nodes over the uplink:
  * the frontends are the cluster's real outbound dependency (rathole tunnels /
    external serving), so this tests what actually matters rather than a proxy;
  * they are owned infrastructure, so there is no third-party probe to be
    polite about;
  * "no frontend reachable from THIS node" is a precise per-node signal that
    this uplink is a bad gateway -- and the right discriminator once there is
    more than one ISP.

The frontend set is read live from etcd so it stays current without a redeploy.
Designed not to flap on transient trouble:
  * a missing/down uplink fails fast (local, deterministic);
  * healthy as soon as ONE frontend answers, so a single frontend reboot or
    maintenance window cannot trip it;
  * if etcd itself is unreachable (or no frontends are configured) we fall back
    to next-hop reachability rather than demoting the node on an etcd blip.
"""

import json
import socket
import subprocess
import sys
import threading

DEFAULT_RATHOLE_PORT = 2333
PROBE_TIMEOUT = 2.0   # seconds, per frontend TCP probe
ETCD_TIMEOUT = 3.0    # seconds, hard ceiling on the live etcd read


def parse_rathole_port(config_value, default=DEFAULT_RATHOLE_PORT):
    """Extract the rathole control port from the etcd rathole config value.

    Accepts bytes/str/None (as returned by etcd3 client.get); returns the port
    core nodes connect to, or ``default`` when the value is missing/malformed.
    """
    if not config_value:
        return default
    if isinstance(config_value, (bytes, bytearray)):
        config_value = config_value.decode()
    try:
        addr = json.loads(config_value).get('remote_addr', '')
    except (ValueError, TypeError):
        return default
    host, sep, port = addr.rpartition(':')
    if sep and host and port.isdigit():
        return int(port)
    return default


def frontend_probe_ips(nodes):
    """IPs to probe, from frontend node dicts. Hostname-only entries are skipped
    (the check probes raw IPs over the uplink to avoid a DNS dependency)."""
    return [n['ip'] for n in nodes if n.get('ip')]


def carrier_up(iface):
    """True if the uplink NIC exists and reports carrier. Absent NIC -> False."""
    try:
        with open(f"/sys/class/net/{iface}/carrier") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def tcp_reachable(iface, ip, port, timeout=PROBE_TIMEOUT):
    """True if a TCP connection to ip:port over ``iface`` succeeds. Binding to a
    missing/wrong interface raises OSError (ENODEV / timeout) and returns False,
    so a bad uplink can never pass."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        s.settimeout(timeout)
        s.connect((ip, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def next_hop_reachable(iface):
    """Fallback signal: ICMP the uplink's interface-scoped default-route next
    hop. Used only when the frontend set can't be determined."""
    out = subprocess.run(["ip", "route", "show", "default", "dev", iface],
                         capture_output=True, text=True).stdout.split()
    if "via" not in out:
        return False
    next_hop = out[out.index("via") + 1]
    return subprocess.run(["ping", "-c1", "-W2", "-I", iface, next_hop],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def read_targets(timeout=ETCD_TIMEOUT):
    """Return (probe_ips, rathole_port) read live from etcd.

    Runs in a watchdog thread so a hung etcd connection can't stall keepalived:
    if the read does not finish within ``timeout`` we raise and the caller falls
    back. Raises on any etcd error too.
    """
    box = {}

    def work():
        try:
            from ..common.etcd_utils import get_etcd_client
            from . import frontend_manager
            client = get_etcd_client(max_retries=1, retry_delay=0)
            nodes = frontend_manager.get_frontend_nodes(client)
            cfg, _ = client.get('/cluster/nodes/rathole/config')
            box['result'] = (frontend_probe_ips(nodes), parse_rathole_port(cfg))
        except Exception as e:  # noqa: BLE001 - any failure -> fall back
            box['error'] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if 'result' in box:
        return box['result']
    raise RuntimeError(box.get('error') or f"etcd read timed out after {timeout}s")


def check_gateway(iface):
    """Return (ok: bool, message: str) for the given uplink interface."""
    if not carrier_up(iface):
        return False, f"uplink {iface} absent or no carrier"

    try:
        ips, port = read_targets()
    except Exception as e:  # noqa: BLE001
        if next_hop_reachable(iface):
            return True, f"next hop reachable via {iface} (etcd unavailable: {e})"
        return False, f"etcd unavailable and next hop unreachable via {iface}: {e}"

    if not ips:
        if next_hop_reachable(iface):
            return True, f"next hop reachable via {iface} (no frontends configured)"
        return False, f"no frontends configured and next hop unreachable via {iface}"

    for ip in ips:
        if tcp_reachable(iface, ip, port):
            return True, f"reached frontend {ip}:{port} via {iface}"
    return False, f"no frontend reachable via {iface} ({len(ips)} tried)"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: gateway_health <uplink-interface>", file=sys.stderr)
        return 2
    ok, msg = check_gateway(argv[0])
    print(("Gateway healthy - " if ok else "Gateway unhealthy - ") + msg,
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
