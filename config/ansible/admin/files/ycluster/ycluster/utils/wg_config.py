#!/usr/bin/env python3
"""
WireGuard server and peer configuration management.

etcd Schema:
  /cluster/wg/server -> {privkey, pubkey, port, endpoints, server_ip}
  /cluster/wg/peers/<hostname> -> {pubkey, pubkey_sha256, status, created_at, approved_at}

Peer IP is NOT stored here — it comes from the node allocation at
/cluster/nodes/by-hostname/<hostname>. WG is transport, not a node type.
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, UTC

from ycluster.common.etcd_utils import get_etcd_client

SERVER_KEY = '/cluster/wg/server'
PEER_PREFIX = '/cluster/wg/peers/'
NODE_PREFIX = '/cluster/nodes/by-hostname/'

DEFAULT_PORT = 51820
WG_SUBNET = '10.0.1.0/24'
SERVER_IP = '10.0.1.1'
WG_INTERFACE = 'wg0'
WG_CONF_PATH = '/etc/wireguard/wg0.conf'


def _run(cmd, input=None, check=True, capture=True):
    return subprocess.run(
        cmd, input=input, check=check,
        capture_output=capture, text=True
    )


def _gen_keypair():
    priv = _run(['wg', 'genkey']).stdout.strip()
    pub = _run(['wg', 'pubkey'], input=priv).stdout.strip()
    return priv, pub


def _fingerprint(pubkey):
    return hashlib.sha256(pubkey.encode()).hexdigest()[:16]


def _parse_endpoint(s):
    """Accept 'host' or 'host:port'. Returns 'host:port'."""
    if ':' in s:
        host, port_str = s.rsplit(':', 1)
        if not host:
            raise ValueError(f"invalid endpoint: '{s}'")
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError(f"invalid port: {port}")
        return f"{host}:{port}"
    return f"{s}:{DEFAULT_PORT}"


def get_server():
    client = get_etcd_client()
    value, _ = client.get(SERVER_KEY)
    if not value:
        return None
    return json.loads(value.decode())


def init_server(endpoints, port=None, rotate=False):
    """Create or update the server record. Endpoints is a list of 'host[:port]'."""
    normalized = [_parse_endpoint(e) for e in endpoints]
    client = get_etcd_client()
    existing = get_server() or {}

    if rotate or 'privkey' not in existing:
        priv, pub = _gen_keypair()
        existing['privkey'] = priv
        existing['pubkey'] = pub

    existing['port'] = port or existing.get('port') or DEFAULT_PORT
    existing['endpoints'] = normalized
    existing['server_ip'] = SERVER_IP
    client.put(SERVER_KEY, json.dumps(existing))
    return existing


def _get_node(hostname):
    client = get_etcd_client()
    value, _ = client.get(f"{NODE_PREFIX}{hostname}")
    if not value:
        return None
    return json.loads(value.decode())


def list_peers():
    """Return [(hostname, peer_record, node_allocation_or_None), ...]."""
    client = get_etcd_client()
    out = []
    for value, meta in client.get_prefix(PEER_PREFIX):
        if not value:
            continue
        hostname = meta.key.decode()[len(PEER_PREFIX):]
        peer = json.loads(value.decode())
        node = _get_node(hostname)
        out.append((hostname, peer, node))
    out.sort(key=lambda x: x[0])
    return out


def get_peer(hostname):
    client = get_etcd_client()
    value, _ = client.get(f"{PEER_PREFIX}{hostname}")
    if not value:
        return None
    return json.loads(value.decode())


def register_peer(hostname, pubkey):
    """Store a pending peer. If the peer already exists with the same pubkey, idempotent.
    If a different pubkey is presented for an existing approved peer, reject."""
    if not _get_node(hostname):
        raise ValueError(f"no node allocation for {hostname} — call /api/allocate first")

    client = get_etcd_client()
    existing = get_peer(hostname)
    fp = _fingerprint(pubkey)

    if existing:
        if existing['pubkey'] == pubkey:
            return existing
        if existing['status'] == 'approved':
            raise ValueError(f"{hostname} already approved with a different key")

    record = {
        'pubkey': pubkey,
        'pubkey_sha256': fp,
        'status': 'pending',
        'created_at': datetime.now(UTC).isoformat(),
        'approved_at': None,
    }
    client.put(f"{PEER_PREFIX}{hostname}", json.dumps(record))
    return record


def set_peer_status(hostname, status):
    if status not in ('pending', 'approved', 'revoked'):
        raise ValueError(f"invalid status: {status}")
    client = get_etcd_client()
    peer = get_peer(hostname)
    if not peer:
        raise ValueError(f"no such peer: {hostname}")
    peer['status'] = status
    if status == 'approved':
        peer['approved_at'] = datetime.now(UTC).isoformat()
    client.put(f"{PEER_PREFIX}{hostname}", json.dumps(peer))
    return peer


def delete_peer(hostname):
    """Delete a wg peer AND the underlying node allocation.

    The register flow created both the node allocation (by-hostname +
    by-mac) and the wg peer record in one call, so deletion symmetrically
    removes all three keys. If the hostname is also being used by a
    physical node that happens to share the name, the caller should
    instead use `revoke` (which leaves the allocation intact).
    """
    client = get_etcd_client()

    # Look up the allocation first so we can also zap the by-mac entry
    node_value, _ = client.get(f"{NODE_PREFIX}{hostname}")
    mac = None
    if node_value:
        try:
            mac = json.loads(node_value.decode()).get('mac')
        except Exception:
            mac = None

    client.delete(f"{PEER_PREFIX}{hostname}")
    client.delete(f"{NODE_PREFIX}{hostname}")
    if mac:
        client.delete(f"/cluster/nodes/by-mac/{mac}")


def render_server_config():
    """Render /etc/wireguard/wg0.conf for the server side."""
    server = get_server()
    if not server:
        raise RuntimeError("wg server not initialized — run 'ycluster wg init <endpoint>'")

    # Bind wg0 with a /24 so the kernel auto-installs a connected route
    # for the peer subnet. Without this, replies to peers would fall
    # through to the physical 10.0.0.0/24 interface.
    lines = [
        "[Interface]",
        f"PrivateKey = {server['privkey']}",
        f"ListenPort = {server['port']}",
        f"Address = {server['server_ip']}/24",
        "",
    ]
    for hostname, peer, node in list_peers():
        if peer['status'] != 'approved' or not node:
            continue
        lines += [
            f"# {hostname}",
            "[Peer]",
            f"PublicKey = {peer['pubkey']}",
            f"AllowedIPs = {node['ip']}/32",
            "",
        ]
    return '\n'.join(lines)


def render_client_config(hostname):
    """Render a client wg0.conf. PrivateKey is left as __PRIVATE_KEY__
    placeholder — the client injects its own key, never stored server-side."""
    server = get_server()
    if not server:
        raise RuntimeError("wg server not initialized")
    node = _get_node(hostname)
    if not node:
        raise ValueError(f"no node allocation for {hostname}")
    if not server.get('endpoints'):
        raise RuntimeError("wg server has no endpoints configured")

    endpoint = server['endpoints'][0]
    return '\n'.join([
        "[Interface]",
        "PrivateKey = __PRIVATE_KEY__",
        f"Address = {node['ip']}/32",
        "",
        f"# server",
        "[Peer]",
        f"PublicKey = {server['pubkey']}",
        f"Endpoint = {endpoint}",
        f"AllowedIPs = 10.0.0.0/24, {WG_SUBNET}",
        "PersistentKeepalive = 25",
        "",
    ])


def _wg_interface_exists():
    r = subprocess.run(['ip', 'link', 'show', WG_INTERFACE],
                       capture_output=True, text=True)
    return r.returncode == 0


def reconcile(up=False, down=False):
    """Apply server config to the live wg0 interface.

    up:   wg-quick up if not running, then syncconf
    down: wg-quick down if running
    neither: syncconf only if interface exists (silent no-op otherwise)
    """
    import shutil
    if not shutil.which('wg-quick'):
        # Called from an admin host that isn't itself a wg server — harmless.
        print("wg-quick not installed on this host; skipping reconcile")
        return

    if down:
        if _wg_interface_exists():
            _run(['wg-quick', 'down', WG_INTERFACE])
            print("wg0 down")
        return

    config = render_server_config()
    import os, tempfile
    os.makedirs('/etc/wireguard', mode=0o700, exist_ok=True)
    with open(WG_CONF_PATH, 'w') as f:
        f.write(config)
    os.chmod(WG_CONF_PATH, 0o600)

    exists = _wg_interface_exists()
    if not exists:
        if up:
            _run(['wg-quick', 'up', WG_INTERFACE])
            print("wg0 up")
        return

    stripped = _run(['wg-quick', 'strip', WG_INTERFACE]).stdout
    with tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False) as tf:
        tf.write(stripped)
        tmp_path = tf.name
    try:
        _run(['wg', 'syncconf', WG_INTERFACE, tmp_path])
        print("wg0 syncconf applied")
    finally:
        os.unlink(tmp_path)
