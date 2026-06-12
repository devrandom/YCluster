"""
YCluster Admin API Service

etcd Schema Documentation:
==========================

Node Allocations:
- /cluster/nodes/by-mac/{normalized_mac} -> allocation JSON
  * normalized_mac: MAC address with colons/dashes removed, lowercase (e.g., "5847caabcdef")
  * allocation JSON contains: hostname, type, ip, amt_ip, mac (normalized), allocated_at

- /cluster/nodes/by-hostname/{normalized_mac} -> allocation JSON (same as above)
  * hostname: node hostname like "s1", "c5", "m3"

DHCP Leases:
- /cluster/dhcp/leases/{lease_key} -> lease JSON
  * lease JSON contains: ip, mac (non-normalized with colons, e.g., "58:47:ca:ab:cd:ef")

Leadership:
- /cluster/leader/app -> hostname of current storage leader
- /cluster/leader/dhcp -> hostname of current DHCP leader

Node Management:
- /cluster/nodes/{hostname}/drain -> "true" if node is drained

Inventory:
- /cluster/nodes/hardware/{hostname} -> hardware facts JSON (auto-collected)
- /cluster/nodes/asset/{hostname}    -> asset metadata JSON (manually entered)

TLS Configuration:
- /cluster/tls/cert -> PEM certificate data
- /cluster/tls/key -> PEM private key data

MAC Address Formats:
- Normalized: lowercase, no separators (5847caabcdef) - used in etcd keys
- Non-normalized: with colons (58:47:ca:ab:cd:ef) - used in DHCP leases and network tools
"""

import functools
import json
import re
import sys

from flask import Flask, request, jsonify, redirect, render_template, send_from_directory, send_file
import psycopg2
import ntplib
import os
import threading
import time
import subprocess
import socket
import platform
import requests
from datetime import datetime, timedelta, UTC
import dns.resolver
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from jinja2 import Template

from ycluster.common.etcd_utils import get_etcd_client

AUTOINSTALL_USER_DATA_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'user-data.j2')
MACOS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'macos-bootstrap.sh.j2')
NAS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'nas-bootstrap.sh.j2')
NVIDIA_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'nvidia-bootstrap.sh.j2')
WG_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'wg-bootstrap.sh.j2')
WG_MACOS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'wg-bootstrap-macos.sh.j2')

app = Flask(__name__)

# Node type interface configurations (can be overridden via env vars)
# Env var format: NODE_INTERFACES_<TYPE>=cluster:uplink:amt (e.g. NODE_INTERFACES_COMPUTE=en*::)
NODE_TYPE_INTERFACES = {
    'storage': {
        'cluster_interface': 'enp2s0f0np0',
        'uplink_interface': 'enp87s0',
        'amt_interface': 'enp89s0'
    },
    'compute': {
        'cluster_interface': 'enp1s0f0',
        'uplink_interface': 'enp1s0f1',
        'amt_interface': 'enp1s0f2'
    },
    'macos': {
        'cluster_interface': 'en0',
        'uplink_interface': 'en1',
        'amt_interface': 'en2'
    },
    'unknown': {
        'cluster_interface': 'en*',
        'uplink_interface': '',
        'amt_interface': ''
    }
}

# Override interfaces from env vars
for node_type in list(NODE_TYPE_INTERFACES.keys()):
    env_key = f'NODE_INTERFACES_{node_type.upper()}'
    env_val = os.environ.get(env_key)
    if env_val:
        parts = env_val.split(':')
        NODE_TYPE_INTERFACES[node_type] = {
            'cluster_interface': parts[0] if len(parts) > 0 else '',
            'uplink_interface': parts[1] if len(parts) > 1 else '',
            'amt_interface': parts[2] if len(parts) > 2 else ''
        }

# etcd configuration
ETCD_PREFIX = '/cluster/nodes'

# Core nodes are derived dynamically from etcd allocations (see
# get_core_nodes()) so adding a new storage node doesn't require a code
# change. The inventory plugin treats any s\d+ hostname as a core node;
# we mirror that by filtering on type == 'storage'.

# Thread lock for allocation operations
allocation_lock = threading.Lock()

# Hostname route params flow into etcd keys (f"{ETCD_PREFIX}/by-hostname/...");
# validate before interpolation so a crafted URL can't address adjacent etcd
# namespaces. Covers <prefix><number> names and the dynamic dhcp-NNN form.
HOSTNAME_PARAM_RE = re.compile(r'^(?:[a-z]{1,4}[0-9]{1,3}|dhcp-[0-9]{1,3})$')


def validated_hostname(view):
    """Reject requests whose hostname/target_hostname route param doesn't
    look like a cluster hostname, before it reaches any etcd key."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        for key in ('hostname', 'target_hostname'):
            value = kwargs.get(key)
            if value is not None and not HOSTNAME_PARAM_RE.match(value):
                return jsonify({'error': f'invalid hostname: {value!r}'}), 400
        return view(*args, **kwargs)
    return wrapper

# IP allocation configuration (avoiding DHCP range 10.0.0.100-200)
IP_RANGES = {
    's': {'base': 10, 'max': 20},    # Storage: 10.0.0.11-30 (s1-s20)
    'c': {'base': 50, 'max': 20},    # Compute: 10.0.0.51-70 (c1-c20)
    'm': {'base': 90, 'max': 20},    # MacOS: 10.0.0.91-110 (m1-m20)
    'nv': {'base': 110, 'max': 20},  # Nvidia: 10.0.0.111-130 (nv1-nv20)
    'nas': {'base': 130, 'max': 10}, # NAS: 10.0.0.131-140 (nas1-nas10)
    'd': {'base': 200, 'max': 30},   # Dev (remote wg scratch): 10.0.1.201-230 (wg peers only)
}

def determine_ip_from_hostname(hostname, via_wg=False):
    """Generate deterministic IP based on hostname.

    via_wg=True puts the IP in the wg peer subnet (10.0.1.0/24) while
    preserving the per-type hostname numbering. A physical compute c3
    lands at 10.0.0.53; a wg-bootstrapped compute c3 lands at 10.0.1.53.
    """
    if not hostname:
        return None

    # Check if this is an AMT hostname (ends with 'a')
    is_amt = hostname.endswith('a')
    if is_amt:
        hostname = hostname[:-1]  # Strip trailing 'a' for parsing

    # Try multi-char prefixes first (e.g., 'nas'), then single-char
    prefix = None
    num = None
    for p in IP_RANGES:
        if hostname.startswith(p):
            try:
                num = int(hostname[len(p):])
                prefix = p
                break
            except ValueError:
                continue

    if prefix is None:
        return None

    config = IP_RANGES[prefix]

    # Validate number is within range
    if num < 1 or num > config['max']:
        raise ValueError(f"Node number {num} for prefix '{prefix}' exceeds range 1-{config['max']}")

    # Calculate base IP address
    base_ip = config['base'] + num

    if is_amt:
        # AMT interface: use separate 10.10.10.0/24 subnet
        return f"10.10.10.{base_ip}"
    elif via_wg:
        # WG-bootstrapped peers live on a separate /24 so the wg server
        # gets a clean connected route instead of per-peer /32s.
        return f"10.0.1.{base_ip}"
    else:
        # Regular interface
        return f"10.0.0.{base_ip}"

def determine_type_from_mac(mac_address):
    """Determine machine type based on MAC address prefix.

    Used for PXE boot allocation. macOS nodes use explicit type parameter
    via /macos/bootstrap endpoint.
    """
    if not mac_address:
        return 'compute'

    # Normalize MAC address to lowercase and remove separators
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

    # Check for storage prefix (58:47:ca becomes 5847ca)
    if normalized_mac.startswith('5847ca'):
        return 'storage'

    # Default to compute
    return 'compute'

def get_next_hostname(client, node_type):
    """Get the next available hostname for a node type"""
    prefixes = {
        'storage': 's',
        'compute': 'c',
        'macos': 'm',
        'nvidia': 'nv',
        'nas': 'nas',
        'dev': 'd',
    }
    
    prefix = prefixes.get(node_type, 'c')
    
    # Get all existing hostnames of this type
    existing_numbers = []
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/{prefix}"):
        if value:
            hostname = metadata.key.decode().split('/')[-1]
            try:
                num = int(hostname[len(prefix):])
                existing_numbers.append(num)
            except:
                pass
    
    # Find the next available number
    next_num = 1
    if existing_numbers:
        existing_numbers.sort()
        # Find first gap or use max+1
        for i, num in enumerate(existing_numbers):
            if num != i + 1:
                next_num = i + 1
                break
        else:
            next_num = len(existing_numbers) + 1
    
    return f"{prefix}{next_num}"

def get_or_create_allocation(mac_address, node_type=None, via_wg=False):
    """Get existing allocation or create new one for non-normalized MAC address.

    Args:
        mac_address: MAC address (any format)
        node_type: Optional type override ('storage', 'compute', 'macos').
                   If not provided, type is detected from MAC address.
                   If provided and different from existing, the allocation is updated.
        via_wg:    Transport marker — WG-bootstrapped nodes get their IP
                   from 10.0.1.0/24 instead of 10.0.0.0/24. Preserved in
                   the allocation record so list/lookup stays consistent.
    """
    client = get_etcd_client()
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

    # Check if allocation already exists
    existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
    if existing_data[0]:
        data = json.loads(existing_data[0].decode())
        existing_via_wg = bool(data.get('via_wg', False))
        # Some old entries may lack IP or amt_ip, fill them in
        if 'amt_ip' not in data:
            amt_ip_address = determine_ip_from_hostname(data['hostname'] + 'a')
            data['amt_ip'] = amt_ip_address

        # Transport changed (LAN <-> WG): IP is derived from hostname+via_wg,
        # so recompute and rewrite. Otherwise a node bootstrapped over WG after
        # a prior LAN allocation keeps its 10.0.0.x address, which collides
        # with the LAN subnet on the WG server and silently breaks the tunnel.
        if via_wg != existing_via_wg:
            new_ip = determine_ip_from_hostname(data['hostname'], via_wg=via_wg)
            print(f"allocation {data['hostname']} ({normalized_mac}): transport "
                  f"{'LAN->WG' if via_wg else 'WG->LAN'}, ip {data['ip']} -> {new_ip}",
                  file=sys.stderr)
            data['ip'] = new_ip
            data['via_wg'] = via_wg
            data['updated_at'] = datetime.now(UTC).isoformat()
            existing_via_wg = via_wg
            allocation_json = json.dumps(data)
            client.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json)
            client.put(f"{ETCD_PREFIX}/by-hostname/{data['hostname']}", allocation_json)

        # Update type if explicitly requested and different
        if node_type and data.get('type') != node_type:
            old_hostname = data['hostname']
            # Allocate new hostname for the new type
            new_hostname = get_next_hostname(client, node_type)
            new_ip = determine_ip_from_hostname(new_hostname, via_wg=existing_via_wg)
            new_amt_ip = determine_ip_from_hostname(new_hostname + "a")

            # Update allocation data
            data['hostname'] = new_hostname
            data['type'] = node_type
            data['ip'] = new_ip
            data['amt_ip'] = new_amt_ip
            data['updated_at'] = datetime.now(UTC).isoformat()

            # Update in etcd (delete old hostname entry, create new one)
            allocation_json = json.dumps(data)
            client.delete(f"{ETCD_PREFIX}/by-hostname/{old_hostname}")
            client.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json)
            client.put(f"{ETCD_PREFIX}/by-hostname/{new_hostname}", allocation_json)

        return data


    # Create new allocation
    with allocation_lock:
        # Double-check after acquiring lock
        existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
        if existing_data[0]:
            return json.loads(existing_data[0].decode())

        # Determine machine type and allocate hostname
        machine_type = node_type or determine_type_from_mac(mac_address)

        # allocation_lock only serializes this process; the DHCP server (and
        # any other admin-api instance) allocates concurrently, so commit via
        # compare-and-swap and retry on conflict.
        for _ in range(5):
            hostname = get_next_hostname(client, machine_type)
            ip_address = determine_ip_from_hostname(hostname, via_wg=via_wg)
            amt_ip_address = determine_ip_from_hostname(hostname + "a")

            allocation_data = {
                'hostname': hostname,
                'type': machine_type,
                'ip': ip_address,
                'amt_ip': amt_ip_address,
                'mac': normalized_mac,
                'via_wg': via_wg,
                'allocated_at': datetime.now(UTC).isoformat()
            }

            allocation_json = json.dumps(allocation_data)
            committed, _ = client.transaction(
                compare=[
                    client.transactions.version(f"{ETCD_PREFIX}/by-mac/{normalized_mac}") == 0,
                    client.transactions.version(f"{ETCD_PREFIX}/by-hostname/{hostname}") == 0
                ],
                success=[
                    client.transactions.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json),
                    client.transactions.put(f"{ETCD_PREFIX}/by-hostname/{hostname}", allocation_json)
                ],
                failure=[]
            )
            if committed:
                return allocation_data

            # Lost the race. If this MAC got allocated elsewhere, return that
            # allocation; otherwise the hostname was taken — pick the next.
            existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
            if existing_data[0]:
                return json.loads(existing_data[0].decode())

        raise RuntimeError(
            f"allocation for {normalized_mac} failed: etcd transaction "
            f"conflicted on every attempt")

@app.route('/api/allocate')
def allocate_hostname():
    """Allocate a new hostname based on MAC address.

    Query parameters:
        mac: MAC address (required)
        type: Optional type override ('storage', 'compute', 'macos')
    """
    mac_address = request.args.get('mac')
    node_type = request.args.get('type')

    if not mac_address:
        return jsonify({'error': 'MAC address is required'}), 400

    if node_type and node_type not in ('storage', 'compute', 'macos', 'nas', 'nvidia', 'dev'):
        return jsonify({'error': f'Invalid type: {node_type}'}), 400

    try:
        allocation = get_or_create_allocation(mac_address, node_type=node_type)

        return jsonify({
            'hostname': allocation['hostname'],
            'type': allocation['type'],
            'ip': allocation['ip'],
            'amt_ip': allocation['amt_ip'],
            'mac': mac_address,
            'existing': True  # Always true since we return existing or newly created
        })
    except Exception as e:
        return jsonify({'error': f'Allocation failed: {str(e)}'}), 500

@app.route('/api/wg/register', methods=['POST'])
def wg_register():
    """Allocate a hostname+IP (as a cluster node of the requested type)
    and register a WireGuard peer pubkey for it in one atomic call.

    This endpoint is public (exposed via the admin-api reverse proxy for
    remote-node onboarding), so it validates input carefully and does not
    leak details of unrelated allocations.

    Body (JSON): {mac, type, pubkey}
      mac:    client MAC (any format)
      type:   one of storage|compute|macos|nas|nvidia (default: compute)
      pubkey: base64-encoded 32-byte WireGuard public key
    Returns: {hostname, ip, status, pubkey_sha256}
    """
    import base64
    from ycluster.utils import wg_config

    data = request.get_json(silent=True) or {}
    mac = data.get('mac')
    node_type = data.get('type') or 'compute'
    pubkey = data.get('pubkey')

    if not mac or not pubkey:
        return jsonify({'error': 'mac and pubkey are required'}), 400
    if node_type not in ('storage', 'compute', 'macos', 'nas', 'nvidia', 'dev'):
        return jsonify({'error': f'invalid type: {node_type}'}), 400

    # Sanity-check the pubkey shape before touching etcd
    try:
        decoded = base64.b64decode(pubkey, validate=True)
        if len(decoded) != 32:
            raise ValueError('wrong length')
    except Exception:
        return jsonify({'error': 'invalid pubkey (expected base64 32-byte key)'}), 400

    try:
        allocation = get_or_create_allocation(mac, node_type=node_type, via_wg=True)
    except Exception as e:
        return jsonify({'error': f'allocation failed: {e}'}), 500

    try:
        peer = wg_config.register_peer(allocation['hostname'], pubkey)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'registration failed: {e}'}), 500

    return jsonify({
        'hostname': allocation['hostname'],
        'ip': allocation['ip'],
        'status': peer['status'],
        'pubkey_sha256': peer['pubkey_sha256'],
    })


@app.route('/api/wg/poll/<hostname>')
@validated_hostname
def wg_poll(hostname):
    """Poll for approval status. Requires fp query param (pubkey_sha256)
    to prevent unauthenticated enumeration of peer configs."""
    from ycluster.utils import wg_config

    fp = request.args.get('fp')
    if not fp:
        return jsonify({'error': 'fp query parameter is required'}), 400

    peer = wg_config.get_peer(hostname)
    if not peer:
        return jsonify({'error': 'no such peer'}), 404
    if peer.get('pubkey_sha256') != fp:
        return jsonify({'error': 'fingerprint mismatch'}), 403

    resp = {'status': peer['status']}
    if peer['status'] == 'approved':
        try:
            resp['config'] = wg_config.render_client_config(hostname)
        except Exception as e:
            return jsonify({'error': f'render failed: {e}'}), 500
    return jsonify(resp)


@app.route('/api/status')
def status():
    """Get current allocation counts by type"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {str(e)}'}), 503
    
    counts = {'storage': 0, 'compute': 0, 'macos': 0}
    
    # Count allocations by type
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                node_type = allocation.get('type', 'compute')
                counts[node_type] = counts.get(node_type, 0) + 1
            except:
                pass
    
    return jsonify(counts)

@app.route('/api/allocations')
def allocations():
    """Get all current allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {str(e)}'}), 503
    
    allocations = []
    
    # Get all allocations from by-hostname (to avoid duplicates)
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                allocations.append({
                    'mac': allocation['mac'],
                    'hostname': allocation['hostname'],
                    'type': allocation['type'],
                    'ip': allocation['ip'],
                    'allocated_at': allocation.get('allocated_at', 0),
                    'disabled': allocation.get('disabled', False)
                })
            except:
                pass
    
    # Sort by hostname
    allocations.sort(key=lambda x: (x['type'], int(x['hostname'][1:]) if x['hostname'][1:].isdigit() else 0))
    
    return jsonify(allocations)


# Cluster mutations (disable/enable, drain, asset edits) are CLI-only:
# the ycluster CLI writes etcd directly, authenticated by the cluster-CA
# etcd client certificate. The admin API serves reads and the TOFU
# bootstrap surface (/api/allocate, /bootstrap/*, /autoinstall/*).

@app.route('/api/dhcp-config')
def get_dhcp_config():
    """Generate DHCP configuration from etcd allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return f"# etcd connection failed: {str(e)}\n", 503
    
    dhcp_config = []
    
    # Get all allocations
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                mac = allocation['mac']
                hostname = allocation['hostname']
                ip = allocation['ip']
                
                # Convert normalized MAC back to colon format
                mac_formatted = ':'.join(mac[i:i+2] for i in range(0, 12, 2))
                dhcp_config.append(f"dhcp-host={mac_formatted},{hostname},{ip},infinite")
            except:
                pass
    
    if dhcp_config:
        return '\n'.join(sorted(dhcp_config)) + '\n', 200, {'Content-Type': 'text/plain'}
    else:
        return "# No static hosts configured yet\n", 200, {'Content-Type': 'text/plain'}

@app.route('/api/hosts')
def get_hosts():
    """Generate hosts file format from etcd allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return f"# etcd connection failed: {str(e)}\n", 503
    
    hosts_entries = []
    
    # Get all allocations
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                hostname = allocation['hostname']
                ip = allocation['ip']
                
                # Skip AMT hostnames registered as nodes - they get correct
                # entries auto-generated from the base node below
                if hostname.endswith('a') and determine_ip_from_hostname(hostname):
                    continue
                
                # Add main hostname entry
                hosts_entries.append(f"{ip} {hostname} {hostname}.xc")
                
                # Add AMT hostname entry if this is a regular node (not already AMT)
                if not hostname.endswith('a'):
                    amt_hostname = f"{hostname}a"
                    amt_ip = determine_ip_from_hostname(amt_hostname)
                    if amt_ip:
                        hosts_entries.append(f"{amt_ip} {amt_hostname} {amt_hostname}.xc")
            except:
                pass
    
    # Frontend nodes live under a separate etcd prefix (not by-hostname) and
    # sit outside the cluster subnet — emit their reachable address so the
    # cluster can resolve/ping/ssh them by name. Only IP-registered nodes can
    # go in a hosts file; hostname-registered ones already resolve via DNS.
    for value, metadata in client.get_prefix("/cluster/nodes/frontend/"):
        if value:
            try:
                node = json.loads(value.decode())
                name = node.get('name')
                ip = node.get('ip')
                if name and ip:
                    hosts_entries.append(f"{ip} {name} {name}.xc")
            except:
                pass

    # Add service aliases that point to storage VIP
    hosts_entries.append("10.0.0.100 registry.xc")
    hosts_entries.append("10.0.0.100 admin.xc")
    hosts_entries.append("10.0.0.100 inference.xc")
    hosts_entries.append("10.0.0.100 auth.xc")

    if hosts_entries:
        return '\n'.join(sorted(hosts_entries)) + '\n', 200, {'Content-Type': 'text/plain'}
    else:
        return "# No static hosts configured yet\n", 200, {'Content-Type': 'text/plain'}

def check_service_status(service_name):
    """Check if a systemd service is active"""
    try:
        result = subprocess.run(['systemctl', 'is-active', service_name], 
                              capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == 'active'
    except:
        return False

def check_port_open(host, port, timeout=3):
    """Check if a port is open on a host"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def check_ceph_status():
    """Check Ceph cluster health"""
    try:
        # Read-only ceph identity provisioned by setup-web-services.yml;
        # keyring is root:admin-api 640, so no privileges needed.
        result = subprocess.run(['ceph', '-n', 'client.admin-api',
                                 '-k', '/etc/ceph/ceph.client.admin-api.keyring',
                                 'health'],
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            health_output = result.stdout.strip()
            if 'HEALTH_OK' in health_output:
                status = 'healthy'
            elif 'HEALTH_ERR' in health_output:
                status = 'unhealthy'
            else:
                status = 'degraded'
            return {
                'status': status,
                'details': health_output
            }
        else:
            return {'status': 'error', 'details': result.stderr.strip()}
    except:
        return {'status': 'unavailable', 'details': 'ceph command failed'}

def check_dns_status():
    """Check DNS (dnsmasq) service and functionality"""
    try:
        # Check if dnsmasq service is running
        service_running = check_service_status('dnsmasq')
        
        # Test local DNS server directly using dnspython
        dns_working = False
        dns_details = "DNS query failed"
        
        try:
            # Create a resolver that queries the local DNS server directly
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ['127.0.0.1']
            resolver.timeout = 3
            resolver.lifetime = 5
            
            # Query local hostname A record
            local_hostname = platform.node()
            answer = resolver.resolve(local_hostname, 'A')
            if answer:
                resolved_ips = [str(rdata) for rdata in answer]
                dns_working = True
                dns_details = f"Local DNS server responding ({local_hostname} -> {', '.join(resolved_ips)})"
            else:
                dns_details = f"Local DNS query for {local_hostname} returned no results"
                
        except dns.resolver.Timeout:
            dns_details = "Local DNS query timeout"
        except dns.resolver.NXDOMAIN:
            dns_details = "Local DNS query: domain not found"
        except dns.resolver.NoAnswer:
            dns_details = "Local DNS query: no answer"
        except Exception as e:
            dns_details = f"Local DNS query error: {str(e)}"
        
        # Overall status
        if service_running and dns_working:
            status = 'healthy'
            details = f"Service active, {dns_details}"
        elif service_running:
            status = 'degraded'
            details = f"Service active but {dns_details}"
        else:
            status = 'unhealthy'
            details = f"Service inactive, {dns_details}"
            
        return {
            'status': status,
            'details': {
                'service_active': service_running,
                'dns_working': dns_working,
                'message': details
            }
        }
    except Exception as e:
        return {'status': 'error', 'details': f'DNS check failed: {str(e)}'}

def check_certificate_expiry():
    """Check TLS certificate expiry from etcd"""
    # The cluster TLS cert lives in etcd and is core-only. Non-storage nodes
    # hold no etcd client (etcd is firewalled to s*), and this is a cluster-wide
    # metric the core nodes already report, so skip it off-core.
    if not is_etcd_node():
        return {
            'status': 'not_applicable',
            'details': {
                'message': 'certificate check is core-only (s*)',
                'days_until_expiry': None,
                'expires_at': None
            }
        }
    try:
        client = get_etcd_client()
        cert_value, _ = client.get('/cluster/tls/cert')
        
        if not cert_value:
            return {
                'status': 'not_configured',
                'details': {
                    'message': 'No certificate found in etcd',
                    'days_until_expiry': None,
                    'expires_at': None
                }
            }
        
        # Parse the certificate
        cert_pem = cert_value.decode()
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        
        # Get expiry date
        expires_at = cert.not_valid_after
        now = datetime.now(UTC).replace(tzinfo=None)  # Remove timezone for comparison
        
        # Calculate days until expiry
        time_until_expiry = expires_at - now
        days_until_expiry = time_until_expiry.days
        
        # Determine status based on days remaining
        if days_until_expiry < 0:
            status = 'expired'
            message = f'Certificate expired {abs(days_until_expiry)} days ago'
        elif days_until_expiry <= 7:
            status = 'critical'
            message = f'Certificate expires in {days_until_expiry} days'
        elif days_until_expiry <= 30:
            status = 'warning'
            message = f'Certificate expires in {days_until_expiry} days'
        else:
            status = 'healthy'
            message = f'Certificate expires in {days_until_expiry} days'
        
        return {
            'status': status,
            'details': {
                'message': message,
                'days_until_expiry': days_until_expiry,
                'expires_at': expires_at.isoformat(),
                'subject': cert.subject.rfc4514_string(),
                'issuer': cert.issuer.rfc4514_string()
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {
                'message': f'Certificate check failed: {str(e)}',
                'days_until_expiry': None,
                'expires_at': None
            }
        }

def check_clock_skew():
    """Check clock skew using NTP protocol to VIP"""

    # NTP server to check against (VIP)
    ntp_server = '10.0.0.254'

    try:
        # Create NTP client
        client = ntplib.NTPClient()
        
        # Make NTP request (this is a lightweight UDP request)
        response = client.request(ntp_server, version=3, timeout=2)
        
        # Get offset in milliseconds
        offset_ms = response.offset * 1000
        
        # Determine status based on offset
        if abs(offset_ms) > 1000:  # More than 1 second
            status = 'critical'
        elif abs(offset_ms) > 100:  # More than 100ms
            status = 'warning'
        else:
            status = 'healthy'
        
        return {
            'status': status,
            'details': {
                'offset_ms': round(offset_ms, 3),
                'ntp_server': ntp_server,
                'stratum': response.stratum,
                'precision': response.precision,
                'delay': response.delay,
                'message': f'Clock offset: {round(offset_ms, 3)}ms'
            }
        }
        
    except ntplib.NTPException as e:
        return {
            'status': 'error',
            'details': {'message': f'NTP request failed: {str(e)}'}
        }
    except socket.gaierror:
        return {
            'status': 'error',
            'details': {'message': f'Could not resolve NTP server {ntp_server}'}
        }
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Clock skew check failed: {str(e)}'}
        }

def check_docker_daemon():
    """Check Docker daemon status"""
    try:
        # systemd unit state is as much as an unprivileged checker can see:
        # the docker API socket is root/docker-group only, and docker-group
        # membership is root-equivalent — not worth it for a version string.
        docker_service_running = check_service_status('docker')

        if docker_service_running:
            status = 'healthy'
            message = 'Docker service running'
        else:
            status = 'unhealthy'
            message = 'Docker service not running'

        return {
            'status': status,
            'details': {
                'service_active': docker_service_running,
                'message': message
            }
        }

    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Docker check failed: {str(e)}'}
        }

def check_docker_registry():
    """Check Docker registry status and functionality"""
    try:
        # Check if registry service is running
        registry_service_running = check_service_status('docker-registry')
        
        # Also check if registry container is running directly
        registry_container_running = False

        # Check if registry port is open (try both localhost and storage VIP)
        registry_port_open = check_port_open('localhost', 5000)

        # Test registry health endpoint
        registry_healthy = False
        registry_error = None
        registry_version = None
        
        if registry_port_open:
            # Try both localhost and VIP endpoints
            test_urls = ['http://localhost:5000/v2/', 'http://10.0.0.100:5000/v2/']
            for url in test_urls:
                try:
                    health_response = requests.get(url, timeout=5)
                    if health_response.status_code == 200:
                        registry_healthy = True
                        registry_version = health_response.headers.get('Docker-Distribution-Api-Version', 'unknown')
                        break
                    else:
                        registry_error = f'Registry health check returned HTTP {health_response.status_code}'
                except requests.exceptions.Timeout:
                    registry_error = 'Registry health check timeout'
                except requests.exceptions.ConnectionError:
                    registry_error = 'Registry connection failed'
                except Exception as e:
                    registry_error = f'Registry health check failed: {str(e)}'
        
        # Check if this node should be running the registry (storage leader)
        is_storage_lead = is_storage_leader()
        
        # Registry is considered running if either service or container is running
        registry_running = registry_service_running or registry_container_running
        
        # Determine overall status
        if is_storage_lead:
            if registry_running and registry_port_open and registry_healthy:
                status = 'healthy'
                message = f'Registry running and healthy (API version {registry_version})'
            elif registry_running and registry_port_open:
                status = 'degraded'
                message = f'Registry running but health check failed: {registry_error}'
            elif registry_running:
                status = 'unhealthy'
                message = f'Registry running but port not accessible: {registry_error}'
            else:
                status = 'unhealthy'
                message = 'Registry not running'
        else:
            # Not storage leader - registry should not be running
            if registry_running or registry_port_open:
                status = 'unhealthy'
                message = 'Split-brain: Registry running on non-leader'
            else:
                status = 'standby'
                message = 'Registry not required (not storage leader)'
        
        return {
            'status': status,
            'details': {
                'service_active': registry_service_running,
                'container_running': registry_container_running,
                'port_open': registry_port_open,
                'health_check_passed': registry_healthy,
                'api_version': registry_version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': registry_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Registry check failed: {str(e)}'}
        }

def check_tang_service():
    """Check Tang server status and functionality"""
    try:
        # Check if Tang service is running
        tang_service_running = check_service_status('tang-server.service')
        
        # Check if Tang port is open
        tang_port_open = check_port_open('localhost', 8777)
        
        # Test Tang advertisement endpoint
        tang_healthy = False
        tang_error = None
        tang_keys = None
        
        if tang_port_open:
            try:
                adv_response = requests.get('http://localhost:8777/adv', timeout=5)
                if adv_response.status_code == 200:
                    tang_healthy = True
                    # Try to parse the advertisement to count keys
                    try:
                        adv_data = adv_response.json()
                        if isinstance(adv_data, dict) and 'keys' in adv_data:
                            tang_keys = len(adv_data['keys'])
                        else:
                            tang_keys = 'unknown'
                    except:
                        tang_keys = 'unknown'
                else:
                    tang_error = f'Tang advertisement returned HTTP {adv_response.status_code}'
            except requests.exceptions.Timeout:
                tang_error = 'Tang advertisement timeout'
            except requests.exceptions.ConnectionError:
                tang_error = 'Tang connection failed'
            except Exception as e:
                tang_error = f'Tang advertisement failed: {str(e)}'
        
        # Determine overall status
        if tang_service_running and tang_port_open and tang_healthy:
            status = 'healthy'
            message = f'Tang server running and healthy ({tang_keys} keys)'
        elif tang_service_running and tang_port_open:
            status = 'degraded'
            message = f'Tang service running but advertisement failed: {tang_error}'
        elif tang_service_running:
            status = 'unhealthy'
            message = f'Tang service running but port not accessible: {tang_error}'
        else:
            status = 'unhealthy'
            message = 'Tang service not running'
        
        return {
            'status': status,
            'details': {
                'service_active': tang_service_running,
                'port_open': tang_port_open,
                'advertisement_working': tang_healthy,
                'key_count': tang_keys,
                'message': message,
                'error': tang_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Tang check failed: {str(e)}'}
        }

def check_secrets_mount():
    """Check if /secrets is mounted"""
    try:
        # Check if /secrets is mounted
        result = subprocess.run(['mountpoint', '-q', '/secrets'], 
                              capture_output=True, text=True, timeout=5)
        is_mounted = result.returncode == 0
        
        # Get mount details if mounted
        mount_details = None
        if is_mounted:
            try:
                mount_result = subprocess.run(['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS', '/secrets'], 
                                            capture_output=True, text=True, timeout=5)
                if mount_result.returncode == 0:
                    mount_details = mount_result.stdout.strip()
            except:
                pass
        
        # Check if secrets directory exists and is accessible
        secrets_accessible = False
        secrets_error = None
        try:
            if os.path.exists('/secrets') and os.path.isdir('/secrets'):
                # Try to list the directory to verify access
                os.listdir('/secrets')
                secrets_accessible = True
            else:
                secrets_error = '/secrets directory does not exist'
        except PermissionError:
            # The service runs unprivileged and /secrets contents are
            # deliberately root-only; mounted-but-unreadable is the
            # expected state, and mountedness is what this check is for.
            secrets_accessible = is_mounted
            secrets_error = None
        except Exception as e:
            secrets_error = f'Error accessing /secrets: {str(e)}'

        # Determine overall status
        if is_mounted and secrets_accessible:
            status = 'healthy'
            message = f'Secrets volume mounted and accessible'
        elif is_mounted:
            status = 'degraded'
            message = f'Secrets volume mounted but not accessible: {secrets_error}'
        else:
            status = 'unhealthy'
            message = 'Secrets volume not mounted'
        
        return {
            'status': status,
            'details': {
                'mounted': is_mounted,
                'accessible': secrets_accessible,
                'mount_details': mount_details,
                'message': message,
                'error': secrets_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Secrets mount check failed: {str(e)}'}
        }

def check_open_webui():
    """Check Open-WebUI service status and functionality"""
    try:
        # Check if Open-WebUI service is running
        webui_service_running = check_service_status('open-webui')
        
        # Check if Open-WebUI port is open
        webui_port_open = check_port_open('localhost', 8380)
        
        # Test Open-WebUI health endpoint
        webui_healthy = False
        webui_error = None
        webui_version = None
        
        if webui_port_open:
            try:
                # Try health check endpoint
                health_response = requests.get('http://localhost:8380/health', timeout=5)
                if health_response.status_code == 200:
                    webui_healthy = True
                    try:
                        health_data = health_response.json()
                        webui_version = health_data.get('version', 'unknown')
                    except:
                        webui_version = 'unknown'
                else:
                    webui_error = f'Open-WebUI health check returned HTTP {health_response.status_code}'
            except requests.exceptions.Timeout:
                webui_error = 'Open-WebUI health check timeout'
            except requests.exceptions.ConnectionError:
                webui_error = 'Open-WebUI connection failed'
            except Exception as e:
                webui_error = f'Open-WebUI health check failed: {str(e)}'
        
        # Check if this node should be running Open-WebUI (storage leader)
        is_storage_lead = is_storage_leader()
        
        # Determine overall status
        if is_storage_lead:
            if webui_service_running and webui_port_open and webui_healthy:
                status = 'healthy'
                message = f'Open-WebUI running and healthy (version {webui_version})'
            elif webui_service_running and webui_port_open:
                status = 'degraded'
                message = f'Open-WebUI running but health check failed: {webui_error}'
            elif webui_service_running:
                status = 'unhealthy'
                message = f'Open-WebUI service running but port not accessible: {webui_error}'
            else:
                status = 'unhealthy'
                message = 'Open-WebUI service not running'
        else:
            # Not storage leader - Open-WebUI should not be running
            if webui_service_running or webui_port_open:
                status = 'unhealthy'
                message = 'Split-brain: Open-WebUI running on non-leader'
            else:
                status = 'standby'
                message = 'Open-WebUI not required (not storage leader)'
        
        return {
            'status': status,
            'details': {
                'service_active': webui_service_running,
                'port_open': webui_port_open,
                'health_check_passed': webui_healthy,
                'version': webui_version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': webui_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Open-WebUI check failed: {str(e)}'}
        }

def is_storage_leader():
    """Check if this node is the current storage leader"""
    if not is_etcd_node():
        return False
    try:
        client = get_etcd_client()
        result = client.get('/cluster/leader/app')
        if result[0]:
            leader = result[0].decode()
            return leader == platform.node()
        return False
    except:
        return False

def is_dhcp_leader():
    """Check if this node is the current DHCP leader"""
    if not is_etcd_node():
        return False
    try:
        client = get_etcd_client()
        result = client.get('/cluster/leader/dhcp')
        if result[0]:
            leader = result[0].decode()
            return leader == platform.node()
        return False
    except:
        return False

def is_node_drained():
    """Check if this node is drained"""
    if not is_etcd_node():
        return False
    try:
        hostname = platform.node()
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{hostname}/drain')
        return result[0] is not None and result[0].decode() == 'true'
    except:
        return False

def get_current_node_type():
    """Determine the current node type based on hostname prefix"""
    hostname = platform.node()
    if hostname and len(hostname) > 0:
        prefix = hostname[0]
        if prefix == 's':
            return 'storage'
        elif prefix == 'c':
            return 'compute'
        elif prefix == 'm':
            return 'macos'
    return 'unknown'

def is_etcd_node():
    """True if this node may talk to etcd (storage nodes, s*).

    etcd is being locked down to s* only (see docs/design/etcd-access-hardening.md).
    Non-storage admin-api instances (compute/nvidia/nas/macos/adhoc) must not
    build an etcd client: their only etcd use was reporting leadership/connectivity
    telemetry that is always trivially false off-core. Gating here keeps those
    instances etcd-free so etcd can be firewalled to s*."""
    return get_current_node_type() == 'storage'

def check_service_conditionally(health_status, service_name, check_func, required_on_storage_only=True):
    """
    Helper function to conditionally check a service based on node type.
    
    Args:
        health_status: The health status dict to update
        service_name: Name of the service being checked
        check_func: Function to call to check the service (should return a dict with 'status' key)
        required_on_storage_only: If True, service is only checked on storage nodes
    """
    is_storage_node = get_current_node_type() == 'storage'
    
    if not required_on_storage_only or is_storage_node:
        service_result = check_func()
        health_status['services'][service_name] = service_result
        
        # Update overall health based on service status
        if service_result['status'] in ['unhealthy', 'error']:
            health_status['overall'] = 'unhealthy'
        elif service_result['status'] == 'degraded' and health_status['overall'] == 'healthy':
            health_status['overall'] = 'degraded'
    else:
        health_status['services'][service_name] = {
            'status': 'not_applicable',
            'details': {'reason': 'not required on compute nodes'}
        }

@app.route('/api/drain/status')
def drain_status():
    """Check drain status of this node"""
    try:
        hostname = platform.node()
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{hostname}/drain')
        is_drained = result[0] is not None and result[0].decode() == 'true'
        return jsonify({'hostname': hostname, 'drained': is_drained})
    except Exception as e:
        return jsonify({'error': f'Failed to check drain status: {str(e)}'}), 500

@app.route('/api/drain/status/<target_hostname>')
@validated_hostname
def drain_status_target(target_hostname):
    """Check drain status of a specific node"""
    try:
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{target_hostname}/drain')
        is_drained = result[0] is not None and result[0].decode() == 'true'
        return jsonify({'hostname': target_hostname, 'drained': is_drained})
    except Exception as e:
        return jsonify({'error': f'Failed to check drain status for {target_hostname}: {str(e)}'}), 500

@app.route('/api/inventory/hardware/<hostname>')
@validated_hostname
def inventory_get_hardware(hostname):
    """Return hardware facts for a node (read-only, internal only)"""
    try:
        from ycluster.utils.inventory import get_hardware
        data = get_hardware(hostname)
        if data is None:
            return jsonify({'error': f'No hardware data for {hostname}'}), 404
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory/asset/<hostname>', methods=['GET'])
@validated_hostname
def inventory_get_asset(hostname):
    """Return asset metadata for a node (internal only)"""
    try:
        from ycluster.utils.inventory import get_asset
        return jsonify(get_asset(hostname))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory')
def inventory_list():
    """Return full inventory (hardware + asset) for all nodes (internal only)"""
    try:
        from ycluster.utils.inventory import list_all
        rows = list_all()
        out = []
        for r in rows:
            alloc = r['allocation'] or {}
            out.append({
                'hostname': alloc.get('hostname'),
                'type': alloc.get('type'),
                'ip': alloc.get('ip'),
                'hardware': r['hardware'],
                'asset': r['asset'],
            })
        out.sort(key=lambda x: x['hostname'] or '')
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory/export.csv')
def inventory_export_csv():
    """Export full inventory as CSV (internal only)"""
    import csv, io
    try:
        from ycluster.utils.inventory import list_all
        rows = list_all()
        rows.sort(key=lambda r: (r['allocation'] or {}).get('hostname', '') or '')

        fieldnames = [
            'hostname', 'type', 'ip',
            'product', 'serial', 'bios_version',
            'cpu', 'ram_gb', 'disks', 'gpus', 'nics',
            'os', 'kernel',
            'vendor', 'purchased_at', 'warranty_expires', 'cost', 'cost_currency', 'location', 'notes',
            'hw_collected_at', 'asset_updated_at',
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for r in rows:
            alloc = r['allocation'] or {}
            hw = r['hardware'] or {}
            asset = r['asset'] or {}
            writer.writerow({
                'hostname':         alloc.get('hostname', ''),
                'type':             alloc.get('type', ''),
                'ip':               alloc.get('ip', ''),
                'product':          hw.get('product', ''),
                'serial':           hw.get('serial', ''),
                'bios_version':     hw.get('bios_version', ''),
                'cpu':              hw.get('cpu', ''),
                'ram_gb':           hw.get('ram_gb', ''),
                'disks':            '; '.join(f"{d['name']} {d['size']} {d['type']}" for d in (hw.get('disks') or [])),
                'gpus':             '; '.join(g.get('model') or g.get('vendor', '?') for g in (hw.get('gpus') or [])),
                'nics':             '; '.join(f"{n['name']}" + (f" {n['speed']}" if n.get('speed') else '') for n in (hw.get('nics') or [])),
                'os':               hw.get('os', ''),
                'kernel':           hw.get('kernel', ''),
                'vendor':           asset.get('vendor', ''),
                'purchased_at':     asset.get('purchased_at', ''),
                'warranty_expires': asset.get('warranty_expires', ''),
                'cost':             asset.get('cost', ''),
                'cost_currency':    asset.get('cost_currency', 'EUR'),
                'location':         asset.get('location', ''),
                'notes':            asset.get('notes', ''),
                'hw_collected_at':  (hw.get('collected_at') or '')[:19],
                'asset_updated_at': (asset.get('updated_at') or '')[:19],
            })
        from flask import Response
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename=inventory.csv'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ping')
def ping():
    """Simple ping endpoint for connectivity testing"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now(UTC).isoformat()})

@app.route('/api/time')
def get_time():
    """Get current timestamp for clock synchronization checks"""
    return jsonify({'timestamp': time.time()})

def get_mac_from_ip(client_ip):
    """
    Look up MAC address from IP address using DHCP leases in etcd or neighbor table.
    Return value is non-normalized MAC address (xx:xx:xx:xx:xx:xx) or None if not found.
    """
    if not client_ip:
        return None
    
    client = get_etcd_client()

    # Look through DHCP leases in etcd
    for value, metadata in client.get_prefix('/cluster/dhcp/leases/'):
        if value:
            try:
                lease_data = json.loads(value.decode())
                if lease_data.get('ip') == client_ip:
                    # Return non-normalized MAC (with colons) from lease data
                    return lease_data.get('mac')
            except json.JSONDecodeError:
                # Skip non-JSON entries in the dhcp prefix
                continue

    print("fallback to neighbor table", client_ip, file=sys.stderr)

    # Fallback to neighbor table lookup using ip --json neigh
    result = None
    try:
        result = subprocess.run(['ip', '--json', 'neigh', 'show', client_ip], 
                              capture_output=True, text=True, timeout=5)
        neighbors = json.loads(result.stdout)
        for i, neighbor in enumerate(neighbors):
            print(f"neighbor {i}: {neighbor}", file=sys.stderr)
            if neighbor.get('dst') == client_ip and 'lladdr' in neighbor:
                mac = neighbor['lladdr']
                # Validate MAC format (should be xx:xx:xx:xx:xx:xx)
                if isinstance(mac, str) and len(mac) == 17 and mac.count(':') == 5:
                    return mac
                else:
                    print(f"MAC validation failed", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"neighbor table JSON parse error: {e}", file=sys.stderr)
        print(f"Raw stdout was: {result.stdout}", file=sys.stderr)
    except Exception as e:
        print(f"neighbor table lookup failed for {client_ip}: {e}", file=sys.stderr)

    return None

@app.route('/autoinstall/meta-data')
def serve_meta_data():
    """Serve empty meta-data for autoinstall"""
    return "", 200, {'Content-Type': 'text/plain'}

@app.route('/autoinstall/user-data')
def serve_user_data():
    """Serve dynamically rendered user-data based on client MAC address"""
    # Get MAC from query param (for Docker/dev) or look up from client IP (prod)
    mac_address = request.args.get('mac')
    client_ip = request.environ.get('REMOTE_ADDR') or request.remote_addr

    if mac_address:
        # Normalize MAC format (GRUB uses xx:xx:xx:xx:xx:xx)
        mac_address = mac_address.lower().replace('-', ':')
        print(f"MAC from query param: {mac_address}", file=sys.stderr)
    else:
        # Fall back to IP lookup
        mac_address = get_mac_from_ip(client_ip)
        if not mac_address:
            return f"MAC address not found for client IP {client_ip}", 400
        print(f"MAC from IP lookup: {client_ip} -> {mac_address}", file=sys.stderr)
    
    # Get or create allocation for this MAC address
    allocation_data = get_or_create_allocation(mac_address)
    
    # Use allocation data
    node_type = allocation_data['type']
    hostname = allocation_data['hostname']
    ip_address = allocation_data['ip']
    amt_ip_address = allocation_data['amt_ip']
    
    # Get interface configuration for this node type (fallback to 'unknown')
    interfaces = NODE_TYPE_INTERFACES.get(node_type, NODE_TYPE_INTERFACES['unknown'])
    
    # Get SSH public key content
    ssh_key_path = '/opt/bootstrap-files/ansible_ssh_key.pub'
    with open(ssh_key_path, 'r') as f:
        ssh_key_content = f.read().strip()

    # Get crypted password for ubuntu user (env var takes precedence for dev)
    ubuntu_password = os.environ.get('UBUNTU_PASSWORD_HASH')
    if not ubuntu_password:
        try:
            with open('/etc/shadow', 'r') as f:
                for line in f:
                    fields = line.strip().split(':')
                    if fields[0] == 'ubuntu' and len(fields) > 1:
                        ubuntu_password = fields[1]
                        break
        except (PermissionError, FileNotFoundError):
            raise Exception("Cannot read ubuntu password from /etc/shadow - set UBUNTU_PASSWORD_HASH env var or check permissions")

        if not ubuntu_password:
            raise Exception("Ubuntu user not found in /etc/shadow - set UBUNTU_PASSWORD_HASH env var")

    proxy_url = 'http://10.0.0.254:3128'

    # Log template variables
    print(f"Generating user-data for {hostname}:", file=sys.stderr)
    print(f"  node_type: {node_type}", file=sys.stderr)
    print(f"  ip_address: {ip_address}", file=sys.stderr)
    print(f"  amt_ip_address: {amt_ip_address}", file=sys.stderr)
    print(f"  cluster_interface: {interfaces['cluster_interface']}", file=sys.stderr)
    print(f"  uplink_interface: {interfaces['uplink_interface']}", file=sys.stderr)
    print(f"  amt_interface: {interfaces['amt_interface']}", file=sys.stderr)
    print(f"  proxy_url: {proxy_url}", file=sys.stderr)
    print(f"  ubuntu_password: {'(from env)' if os.environ.get('UBUNTU_PASSWORD_HASH') else '(from shadow)'}", file=sys.stderr)

    # Read and render template
    with open(AUTOINSTALL_USER_DATA_TEMPLATE, 'r') as f:
        template_content = f.read()

    template = Template(template_content)
    rendered_content = template.render(
        node_type=node_type,
        hostname=hostname,
        ip_address=ip_address,
        amt_ip_address=amt_ip_address,
        cluster_interface=interfaces['cluster_interface'],
        uplink_interface=interfaces['uplink_interface'],
        amt_interface=interfaces['amt_interface'],
        ssh_key_content=ssh_key_content,
        ubuntu_password=ubuntu_password,
        proxy_url=proxy_url
    )
    
    return rendered_content, 200, {'Content-Type': 'text/plain'}


BOOTSTRAP_TEMPLATES = {
    'macos': MACOS_BOOTSTRAP_TEMPLATE,
    'nas': NAS_BOOTSTRAP_TEMPLATE,
    'nvidia': NVIDIA_BOOTSTRAP_TEMPLATE,
    'wg': WG_BOOTSTRAP_TEMPLATE,
    'wg-macos': WG_MACOS_BOOTSTRAP_TEMPLATE,
}

# Bootstrap types whose rendered api_server must be the public admin
# subdomain (reverse proxy) rather than the request host. Remote flows
# only — local node types keep using request.host.
PUBLIC_BOOTSTRAP_TYPES = {'wg', 'wg-macos'}

@app.route('/bootstrap/')
def serve_bootstrap_index():
    """Serve bootstrap usage hints"""
    api_server = f"http://{request.host}"
    text = f"""YCluster Bootstrap

Available types:
  macos    - macOS compute nodes (local cluster network)
  nas      - Ubuntu-based NAS devices
  nvidia   - Ubuntu-based Nvidia GPU servers
  wg       - WireGuard overlay client, Linux (remote node joining over the internet)
  wg-macos - WireGuard overlay client, macOS (remote node joining over the internet)

Usage:
  curl {api_server}/bootstrap/<type> | sudo bash

Examples:
  curl {api_server}/bootstrap/macos | sudo bash
  curl {api_server}/bootstrap/nas | sudo bash
  curl {api_server}/bootstrap/nvidia | sudo bash
  curl {api_server}/bootstrap/wg | sudo bash -s -- --type compute
  curl {api_server}/bootstrap/wg | sudo bash -s -- --dev
  curl {api_server}/bootstrap/wg-macos | sudo bash
  curl {api_server}/bootstrap/wg-macos | sudo bash -s -- --dev
"""
    return text, 200, {'Content-Type': 'text/plain'}


@app.route('/bootstrap/<node_type>')
def serve_bootstrap(node_type):
    """Serve bootstrap script for a given node type"""
    if node_type not in BOOTSTRAP_TEMPLATES:
        return jsonify({'error': f'Unknown bootstrap type: {node_type}', 'available': list(BOOTSTRAP_TEMPLATES.keys())}), 404

    # Determine the API server URL. Public (wg) bootstrap flows must use
    # the public admin subdomain (reverse proxy exposes only a whitelist
    # of paths there). The WG tunnel endpoint is separate and rendered
    # into the client wg config itself. Local node bootstraps (macos/
    # nas/nvidia) keep using the host header.
    if node_type in PUBLIC_BOOTSTRAP_TYPES:
        try:
            etcd = get_etcd_client()
            domain_value, _ = etcd.get('/cluster/https/domain')
        except Exception as e:
            return jsonify({'error': f'etcd lookup failed: {e}'}), 503
        if not domain_value:
            return jsonify({
                'error': f'{node_type} bootstrap requires /cluster/https/domain '
                         'to be set (run `ycluster https set-domain <domain>`)'
            }), 503
        api_server = f"https://admin.{domain_value.decode().strip()}"
    else:
        api_server = f"http://{request.host}"

    # Get SSH public key content
    ssh_key_path = '/opt/bootstrap-files/ansible_ssh_key.pub'
    with open(ssh_key_path, 'r') as f:
        ssh_key_content = f.read().strip()

    # Read and render template
    with open(BOOTSTRAP_TEMPLATES[node_type], 'r') as f:
        template_content = f.read()

    template = Template(template_content)
    rendered_content = template.render(
        api_server=api_server,
        ssh_key_content=ssh_key_content
    )

    # Ensure trailing newline
    if not rendered_content.endswith('\n'):
        rendered_content += '\n'

    return rendered_content, 200, {'Content-Type': 'text/plain'}


@app.route('/metrics')
def prometheus_metrics():
    """Prometheus metrics endpoint"""
    try:
        # Get health data
        health_data = get_comprehensive_health()
        
        metrics = []
        
        # Overall health metric
        overall_value = 1 if health_data['overall'] == 'healthy' else 0
        metrics.append(f'ycluster_node_healthy{{node="{platform.node()}"}} {overall_value}')
        
        # Service health metrics
        for service, details in health_data.get('services', {}).items():
            status = details.get('status')
            if status == 'healthy':
                service_value = 0
            elif status == 'degraded':
                service_value = 1
            elif status == 'unhealthy':
                service_value = 2
            elif status == 'standby':
                service_value = 3
            else:
                service_value = 2
            metrics.append(f'ycluster_service_health{{node="{platform.node()}",service="{service}"}} {service_value}')
            
            # Service-specific metrics
            if service == 'ceph' and isinstance(details.get('details'), dict):
                # Ceph status could be healthy/degraded/unhealthy
                ceph_status = details['details']
                if ceph_status == 'HEALTH_OK':
                    metrics.append(f'ycluster_ceph_health{{node="{platform.node()}"}} 1')
                else:
                    metrics.append(f'ycluster_ceph_health{{node="{platform.node()}"}} 0')
        
        # Leadership metrics
        storage_leader = 1 if health_data.get('storage_leader', False) else 0
        dhcp_leader = 1 if health_data.get('dhcp_leader', False) else 0
        metrics.append(f'ycluster_storage_leader{{node="{platform.node()}"}} {storage_leader}')
        metrics.append(f'ycluster_dhcp_leader{{node="{platform.node()}"}} {dhcp_leader}')
        
        # VIP metrics
        vip_status = check_vip_status()
        gateway_vip_active = 1 if vip_status['gateway_vip']['active'] else 0
        storage_vip_active = 1 if vip_status['storage_vip']['active'] else 0
        metrics.append(f'ycluster_vip_active{{node="{platform.node()}",vip="gateway"}} {gateway_vip_active}')
        metrics.append(f'ycluster_vip_active{{node="{platform.node()}",vip="storage"}} {storage_vip_active}')
        
        # Certificate expiry metrics
        cert_status = check_certificate_expiry()
        if cert_status.get('details', {}).get('days_until_expiry') is not None:
            days_until_expiry = cert_status['details']['days_until_expiry']
            metrics.append(f'ycluster_certificate_days_until_expiry{{node="{platform.node()}"}} {days_until_expiry}')
        
        # Node drain status
        drained = 1 if health_data.get('drained', False) else 0
        metrics.append(f'ycluster_node_drained{{node="{platform.node()}"}} {drained}')
        
        # Return metrics in Prometheus format
        response = '\n'.join(metrics) + '\n'
        return response, 200, {'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'}
        
    except Exception as e:
        # Return error metric
        error_response = f'ycluster_metrics_error{{node="{platform.node()}"}} 1\n'
        return error_response, 500, {'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'}

def get_comprehensive_health():
    """Get comprehensive health data (extracted from health() function)"""
    health_status = {
        'overall': 'healthy',
        'services': {}
    }
    
    # Determine current node type
    current_node_type = get_current_node_type()
    is_storage_node = current_node_type == 'storage'

    # Check etcd — only storage (s*) nodes talk to etcd. Non-storage admin-api
    # instances hold no etcd client (etcd is firewalled to s*), so probing it
    # here would be both meaningless and a connection we deliberately removed.
    if is_etcd_node():
        try:
            client = get_etcd_client()
            client.get('/test')
            health_status['services']['etcd'] = {'status': 'healthy', 'details': 'connected'}
        except Exception as e:
            health_status['services']['etcd'] = {'status': 'unhealthy', 'details': str(e)}
            health_status['overall'] = 'unhealthy'
    else:
        health_status['services']['etcd'] = {
            'status': 'not_applicable',
            'details': {'reason': 'admin-api uses etcd only on storage nodes'}
        }
    
    # Check Ceph storage (only on storage nodes)
    if is_storage_node:
        ceph_health = check_ceph_status()
        health_status['services']['ceph'] = ceph_health
        if ceph_health['status'] not in ['healthy', 'degraded']:
            health_status['overall'] = 'unhealthy'
    else:
        health_status['services']['ceph'] = {
            'status': 'not_applicable',
            'details': {'reason': 'not required on compute nodes'}
        }
    
    # Check PostgreSQL (always check, flag split-brain if running on non-leader)
    postgres_running = check_service_status('postgresql@16-main')
    postgres_port = check_port_open('localhost', 5432)
    is_storage_lead = is_storage_leader()
    
    if is_storage_lead:
        postgres_healthy = postgres_running and postgres_port
        health_status['services']['postgresql'] = {
            'status': 'healthy' if postgres_healthy else 'unhealthy',
            'details': {
                'service_active': postgres_running,
                'port_open': postgres_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not postgres_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if postgres_running or postgres_port:
            health_status['services']['postgresql'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': postgres_running,
                    'port_open': postgres_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['postgresql'] = {
                'status': 'standby',
                'details': {
                    'service_active': postgres_running,
                    'port_open': postgres_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check Qdrant (always check, flag split-brain if running on non-leader)
    qdrant_running = check_service_status('qdrant')
    qdrant_port = check_port_open('localhost', 6333)
    
    if is_storage_lead:
        qdrant_healthy = qdrant_running and qdrant_port
        health_status['services']['qdrant'] = {
            'status': 'healthy' if qdrant_healthy else 'unhealthy',
            'details': {
                'service_active': qdrant_running,
                'port_open': qdrant_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not qdrant_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if qdrant_running or qdrant_port:
            health_status['services']['qdrant'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': qdrant_running,
                    'port_open': qdrant_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['qdrant'] = {
                'status': 'standby',
                'details': {
                    'service_active': qdrant_running,
                    'port_open': qdrant_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check storage-only services (storage_leader_election, dhcp_leader_election)
    for service_name, systemd_name in [
        ('storage_leader_election', 'storage-leader-election'),
        ('dhcp_leader_election', 'dhcp-leader-election')
    ]:
        if is_storage_node:
            service_running = check_service_status(systemd_name)
            health_status['services'][service_name] = {
                'status': 'healthy' if service_running else 'unhealthy',
                'details': {'service_active': service_running}
            }
            if not service_running:
                health_status['overall'] = 'unhealthy'
        else:
            health_status['services'][service_name] = {
                'status': 'not_applicable',
                'details': {'reason': 'not required on compute nodes'}
            }
    
    # Check DHCP (only required if we are DHCP leader)
    is_dhcp_lead = is_dhcp_leader()
    dhcp_port = check_port_open('localhost', 8067)  # DHCP health port
    
    if is_dhcp_lead:
        health_status['services']['dhcp'] = {
            'status': 'healthy' if dhcp_port else 'unhealthy',
            'details': {
                'health_port_open': dhcp_port,
                'required': True,
                'reason': 'dhcp leader'
            }
        }
        if not dhcp_port:
            health_status['overall'] = 'unhealthy'
    else:
        # Check for split-brain condition
        if dhcp_port:
            health_status['services']['dhcp'] = {
                'status': 'unhealthy',
                'details': {
                    'health_port_open': dhcp_port,
                    'required': False,
                    'reason': 'split-brain: dhcp running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['dhcp'] = {
                'status': 'standby',
                'details': {
                    'health_port_open': dhcp_port,
                    'required': False,
                    'reason': 'not dhcp leader'
                }
            }
    
    # Check DNS (dnsmasq)
    dns_health = check_dns_status()
    health_status['services']['dns'] = dns_health
    if dns_health['status'] == 'unhealthy':
        health_status['overall'] = 'unhealthy'
    
    # Check Squid proxy
    squid_running = check_service_status('squid')
    squid_port = check_port_open('localhost', 3128)
    squid_functional = False
    squid_error = None
    
    if squid_running and squid_port:
        # Test actual proxy functionality using local ping endpoint
        try:
            # Test a simple HTTP request through the proxy to our own ping endpoint
            proxy_response = requests.get(
                'http://localhost:12723/api/ping',
                proxies={'http': 'http://localhost:3128'},
                timeout=5
            )
            if proxy_response.status_code in [200, 503]:
                squid_functional = True
            else:
                squid_error = f'HTTP {proxy_response.status_code}'
        except requests.exceptions.ProxyError as e:
            squid_error = f'Proxy error: {str(e)}'
        except requests.exceptions.Timeout:
            squid_error = 'Proxy timeout'
        except Exception as e:
            squid_error = f'Proxy test failed: {str(e)}'
    
    squid_healthy = squid_running and squid_port and squid_functional
    health_status['services']['squid'] = {
        'status': 'healthy' if squid_healthy else 'unhealthy',
        'details': {
            'service_active': squid_running,
            'port_open': squid_port,
            'proxy_functional': squid_functional,
            'error': squid_error
        }
    }
    if not squid_healthy:
        health_status['overall'] = 'unhealthy'
    
    # Check NTP
    ntp_running = check_service_status('ntp') or check_service_status('chrony')
    health_status['services']['ntp'] = {
        'status': 'healthy' if ntp_running else 'unhealthy',
        'details': {'service_active': ntp_running}
    }
    if not ntp_running:
        health_status['overall'] = 'unhealthy'
    
    # Check TLS certificate expiry
    cert_health = check_certificate_expiry()
    health_status['services']['tls_certificate'] = cert_health
    if cert_health['status'] in ['expired', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif cert_health['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check rathole (only required if we are storage leader)
    rathole_running = check_service_status('rathole')
    rathole_port = check_port_open('localhost', 2333)  # Default rathole client port
    
    if is_storage_lead:
        rathole_healthy = rathole_running
        health_status['services']['rathole'] = {
            'status': 'healthy' if rathole_healthy else 'unhealthy',
            'details': {
                'service_active': rathole_running,
                'port_open': rathole_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not rathole_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if rathole_running:
            health_status['services']['rathole'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': rathole_running,
                    'port_open': rathole_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['rathole'] = {
                'status': 'standby',
                'details': {
                    'service_active': rathole_running,
                    'port_open': rathole_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check clock skew
    clock_skew = check_clock_skew()
    health_status['services']['clock_skew'] = clock_skew
    if clock_skew['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif clock_skew['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check Docker daemon (only on storage nodes)
    if is_storage_node:
        docker_daemon = check_docker_daemon()
        health_status['services']['docker_daemon'] = docker_daemon
        if docker_daemon['status'] in ['unhealthy', 'error']:
            health_status['overall'] = 'unhealthy'
        elif docker_daemon['status'] == 'degraded' and health_status['overall'] == 'healthy':
            health_status['overall'] = 'degraded'
    else:
        health_status['services']['docker_daemon'] = {
            'status': 'not_applicable',
            'details': {'reason': 'not required on compute nodes'}
        }
    
    # Check Docker registry
    docker_registry = check_docker_registry()
    health_status['services']['docker_registry'] = docker_registry
    if docker_registry['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif docker_registry['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check storage-only services with complex health checks (tang, secrets_mount)
    for service_name, check_func in [
        ('tang', check_tang_service),
        ('secrets_mount', check_secrets_mount)
    ]:
        if is_storage_node:
            service_result = check_func()
            health_status['services'][service_name] = service_result
            if service_result['status'] in ['unhealthy', 'error']:
                health_status['overall'] = 'unhealthy'
            elif service_result['status'] == 'degraded' and health_status['overall'] == 'healthy':
                health_status['overall'] = 'degraded'
        else:
            health_status['services'][service_name] = {
                'status': 'not_applicable',
                'details': {'reason': 'not required on compute nodes'}
            }
    
    # Check Open-WebUI
    open_webui = check_open_webui()
    health_status['services']['open_webui'] = open_webui
    if open_webui['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif open_webui['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check VIP status
    vip_health = check_vip_status()
    gateway_vip_active = vip_health['gateway_vip']['active']
    storage_vip_active = vip_health['storage_vip']['active']
    
    health_status['services']['gateway_vip'] = {
        'status': 'healthy' if gateway_vip_active else 'standby',
        'details': vip_health['gateway_vip']
    }
    
    health_status['services']['storage_vip'] = {
        'status': 'healthy' if storage_vip_active else 'standby',
        'details': vip_health['storage_vip']
    }
    
    # Check keepalived service (only on core nodes). Short-circuit on
    # non-storage nodes so get_core_nodes() (an etcd read) only runs where an
    # etcd client exists — non-core nodes are never core. etcd is core-only.
    current_hostname = platform.node()
    if is_etcd_node() and current_hostname in get_core_nodes():
        keepalived_running = check_service_status('keepalived')
        health_status['services']['keepalived'] = {
            'status': 'healthy' if keepalived_running else 'unhealthy',
            'details': {'service_active': keepalived_running}
        }
        if not keepalived_running:
            health_status['overall'] = 'unhealthy'
    else:
        # Not a core node - keepalived should not be running
        keepalived_running = check_service_status('keepalived')
        if keepalived_running:
            health_status['services']['keepalived'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': keepalived_running,
                    'reason': 'split-brain: keepalived running on non-core node'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['keepalived'] = {
                'status': 'standby',
                'details': {
                    'service_active': keepalived_running,
                    'reason': 'not a core node'
                }
            }
    
    # Add leadership status for this node
    health_status['storage_leader'] = is_storage_leader()
    health_status['dhcp_leader'] = is_dhcp_leader()
    health_status['drained'] = is_node_drained()
    
    return health_status

@app.route('/api/health')
def health():
    """Comprehensive health check endpoint for all services"""
    health_status = get_comprehensive_health()

    # Return appropriate HTTP status code
    status_code = 200 if health_status['overall'] == 'healthy' else 503
    return jsonify(health_status), status_code

@app.route('/api/alert-webhook', methods=['POST'])
def alert_webhook():
    """Webhook endpoint for Alertmanager notifications"""
    try:
        alert_data = request.get_json()
        
        # Log the alert
        for alert in alert_data.get('alerts', []):
            status = alert.get('status', 'unknown')
            alertname = alert.get('labels', {}).get('alertname', 'unknown')
            severity = alert.get('labels', {}).get('severity', 'unknown')
            node = alert.get('labels', {}).get('node', 'unknown')
            
            print(f"Alert {status}: {alertname} (severity: {severity}, node: {node})")
        
        # Here you could integrate with external notification systems
        # For now, just acknowledge receipt
        return jsonify({'status': 'received'}), 200
        
    except Exception as e:
        print(f"Error processing alert webhook: {e}")
        return jsonify({'error': str(e)}), 500

_STATIC_HOSTNAME_RE = re.compile(r'^([a-z]{1,3})(\d+)$')


def get_core_nodes():
    """Return list of core node hostnames (storage-typed, not disabled).

    Used to decide whether keepalived should be running on a given node
    and which nodes to include in cluster-wide VIP/keepalived checks.
    """
    return [h['hostname'] for h in get_all_hosts()
            if h.get('type') == 'storage' and not h.get('disabled')]


def get_all_hosts():
    """Get all hosts from etcd allocations"""
    try:
        client = get_etcd_client()
        hosts = []

        # Get all allocations from by-hostname
        for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
            if not value:
                continue
            try:
                allocation = json.loads(value.decode())
                hostname = allocation['hostname']

                # Skip AMT interfaces (hostnames ending with 'a').
                if hostname.endswith('a'):
                    continue

                # Keep only static-prefix allocations: letters followed
                # by digits (e.g. s3, c1, m2, nv1, nas1). Dynamic-IP
                # allocations (dhcp-NNN, etc.) don't match and are
                # excluded.
                if not _STATIC_HOSTNAME_RE.match(hostname):
                    continue

                hosts.append({
                    'hostname': hostname,
                    'ip': allocation['ip'],
                    'type': allocation['type'],
                    'disabled': allocation.get('disabled', False)
                })
            except Exception:
                pass

        # Sort by (type, numeric suffix). Prefix length varies (s, nv,
        # nas), so parse the number via the regex instead of assuming
        # hostname[1:].
        def _sort_key(h):
            m = _STATIC_HOSTNAME_RE.match(h['hostname'])
            return (h['type'], int(m.group(2)) if m else 0)

        hosts.sort(key=_sort_key)
        return hosts
    except Exception:
        return []

def get_host_health(host_ip, timeout=10):
    """Get health status from a specific host"""
    try:
        response = requests.get(f"http://{host_ip}:12723/api/health", timeout=timeout)
        if response.status_code in [200, 503]:
            # Both 200 (healthy) and 503 (unhealthy) contain valid health data
            return response.json()
        else:
            return {'overall': 'error', 'services': {}, 'error': f'HTTP {response.status_code}'}
    except requests.exceptions.Timeout:
        return {'overall': 'timeout', 'services': {}, 'error': 'Request timeout'}
    except requests.exceptions.ConnectionError:
        return {'overall': 'unreachable', 'services': {}, 'error': 'Connection failed'}
    except Exception as e:
        return {'overall': 'error', 'services': {}, 'error': str(e)}

def check_vip_status():
    """Check VIP status using keepalived and ip commands"""
    gateway_vip_ip = '10.0.0.254'
    storage_vip_ip = '10.0.0.100'
    vip_status = {
        'gateway_vip': {
            'ip': gateway_vip_ip,
            'active': False,
            'master': None,
            'interface': None
        },
        'storage_vip': {
            'ip': storage_vip_ip,
            'active': False,
            'master': None,
            'interface': None
        }
    }
    
    # Check gateway VIP
    try:
        # Use 'ip -j addr show to <vip>' to get JSON output for reliable parsing
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', gateway_vip_ip], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            # Parse JSON output - if there's any output, VIP is active on this node
            interfaces = json.loads(result.stdout)
            if interfaces:
                # VIP is assigned to this node
                vip_status['gateway_vip']['active'] = True
                vip_status['gateway_vip']['master'] = platform.node()
                # Get interface name from first interface in results
                vip_status['gateway_vip']['interface'] = interfaces[0].get('ifname')
        else:
            # No output means VIP is not assigned to this node
            vip_status['gateway_vip']['active'] = False
            
    except json.JSONDecodeError as e:
        vip_status['gateway_vip']['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        vip_status['gateway_vip']['error'] = str(e)
    
    # Check storage VIP
    try:
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', storage_vip_ip], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            interfaces = json.loads(result.stdout)
            if interfaces:
                vip_status['storage_vip']['active'] = True
                vip_status['storage_vip']['master'] = platform.node()
                vip_status['storage_vip']['interface'] = interfaces[0].get('ifname')
        else:
            vip_status['storage_vip']['active'] = False
            
    except json.JSONDecodeError as e:
        vip_status['storage_vip']['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        vip_status['storage_vip']['error'] = str(e)
    
    # Check keepalived service status
    try:
        keepalived_running = check_service_status('keepalived')
        vip_status['keepalived_service'] = {
            'active': keepalived_running,
            'status': 'running' if keepalived_running else 'stopped'
        }
    except Exception as e:
        vip_status['keepalived_service'] = {
            'active': False,
            'status': 'error',
            'error': str(e)
        }
    
    return vip_status

def get_cluster_vip_status(host_health):
    """Get VIP status across all cluster nodes from existing health data"""
    vip_info = {
        'gateway_vip': {
            'ip': '10.0.0.254',
            'active_on': None,
            'master_hostname': None,
            'interface': None
        },
        'storage_vip': {
            'ip': '10.0.0.100',
            'active_on': None,
            'master_hostname': None,
            'interface': None
        },
        'keepalived_nodes': []  # Single list for keepalived status across all nodes
    }
    
    # Get all hosts to find core nodes
    all_hosts = get_all_hosts()
    
    # Process only core nodes (where keepalived runs and VIPs can be active)
    for core_node in get_core_nodes():
        # Find the core node in all_hosts to get its IP
        core_host = next((host for host in all_hosts if host['hostname'] == core_node), None)
        if not core_host:
            # Core node not found in allocations - add as missing
            vip_info['keepalived_nodes'].append({
                'hostname': core_node,
                'ip': 'unknown',
                'keepalived_active': False,
                'status': 'not_allocated'
            })
            continue
        
        hostname = core_host['hostname']
        host_ip = core_host['ip']
        health_data = host_health.get(core_node, {})
        
        if 'error' in health_data or 'services' not in health_data:
            vip_info['keepalived_nodes'].append({
                'hostname': core_node,
                'ip': host_ip,
                'keepalived_active': False,
                'status': 'unreachable'
            })
            continue
        
        # Process VIP status
        gateway_vip_service = health_data.get('services', {}).get('gateway_vip', {})
        storage_vip_service = health_data.get('services', {}).get('storage_vip', {})
        
        # Process gateway VIP
        gateway_vip_details = gateway_vip_service.get('details', {})
        if gateway_vip_details and gateway_vip_details.get('active', False):
            vip_info['gateway_vip']['active_on'] = host_ip
            vip_info['gateway_vip']['master_hostname'] = hostname
            vip_info['gateway_vip']['interface'] = gateway_vip_details.get('interface')
        
        # Process storage VIP
        storage_vip_details = storage_vip_service.get('details', {})
        if storage_vip_details and storage_vip_details.get('active', False):
            vip_info['storage_vip']['active_on'] = host_ip
            vip_info['storage_vip']['master_hostname'] = hostname
            vip_info['storage_vip']['interface'] = storage_vip_details.get('interface')
        
        # Process keepalived service status
        keepalived_service = health_data.get('services', {}).get('keepalived', {})
        if keepalived_service:
            keepalived_active = keepalived_service.get('details', {}).get('service_active', False)
            keepalived_status = 'running' if keepalived_active else 'stopped'
        else:
            keepalived_active = False
            keepalived_status = 'no_data'
        
        vip_info['keepalived_nodes'].append({
            'hostname': core_node,
            'ip': host_ip,
            'keepalived_active': keepalived_active,
            'status': keepalived_status
        })
    
    return vip_info


def get_leadership_status():
    """Get current leadership status from etcd"""
    try:
        client = get_etcd_client()
        leadership = {}
        
        # Get storage leader
        result = client.get('/cluster/leader/app')
        if result[0]:
            storage_leader = result[0].decode()
            leadership['storage_leader'] = storage_leader
        
        # Get DHCP leader
        result = client.get('/cluster/leader/dhcp')
        if result[0]:
            dhcp_leader = result[0].decode()
            leadership['dhcp_leader'] = dhcp_leader
            
        return leadership
    except:
        return {}

# Prometheus http_sd_configs target definitions.
#
# Each job maps to a port and a predicate (applied to get_all_hosts() output)
# that decides which hosts should be scraped. The admin API is scraped by
# Prometheus itself, so this endpoint can answer target queries dynamically
# on every scrape interval — disabling a host via `ycluster cluster disable`
# immediately removes it from the target list without re-running ansible.
PROMETHEUS_JOB_SPECS = {
    'node-exporter': {
        'port': 9100,
        'predicate': lambda h: h['type'] in ('storage', 'nvidia', 'compute', 'nas', 'macos'),
    },
    'ycluster-health': {
        'port': 12723,
        'predicate': lambda h: h['type'] == 'storage',
    },
    'ycluster-dhcp': {
        'port': 8067,
        'predicate': lambda h: h['type'] == 'storage',
    },
    'ceph-exporter': {
        'port': 9283,
        'predicate': lambda h: h['type'] == 'storage',
    },
}


@app.route('/api/prometheus/targets/<job>')
def prometheus_targets(job):
    """Return Prometheus http_sd target list for a job, minus disabled hosts."""
    spec = PROMETHEUS_JOB_SPECS.get(job)
    if not spec:
        return jsonify({'error': f'unknown job: {job}'}), 404

    targets = []
    for host in get_all_hosts():
        if host.get('disabled'):
            continue
        if not spec['predicate'](host):
            continue
        targets.append({
            'targets': [f"{host['ip']}:{spec['port']}"],
            'labels': {
                'node': host['hostname'],
                'node_type': host['type'],
            },
        })
    return jsonify(targets)


def get_inference_status():
    """Fetch local-ai-proxy /healthz from this node's localhost. Returns
    a dict (per-backend + per-model health) or None if the proxy is not
    running here."""
    try:
        resp = requests.get('http://127.0.0.1:4001/healthz', timeout=3)
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException:
        return None


def _scrub_adhoc_etcd(result):
    """Adhoc nodes are untrusted and never get etcd client certs (etcd is
    restricted to the trusted fleet via mTLS), so ignore any etcd status they
    self-report — older adhoc images probe etcd unconditionally and would
    otherwise always read unhealthy at the enforce phase. Recompute overall only
    if etcd was the sole failure."""
    services = result.get('services')
    if isinstance(services, dict) and 'etcd' in services:
        services['etcd'] = {'status': 'not_applicable',
                            'details': {'reason': 'adhoc nodes are untrusted; no etcd access'}}
        if result.get('overall') == 'unhealthy' and not any(
                isinstance(v, dict) and v.get('status') == 'unhealthy'
                for v in services.values()):
            result['overall'] = 'healthy'
    return result


def get_all_host_health(hosts):
    """Fetch health for all non-disabled hosts in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    host_health = {}
    active = [h for h in hosts if not h.get('disabled', False)]
    types = {h['hostname']: h.get('type') for h in hosts}
    for h in hosts:
        if h.get('disabled', False):
            host_health[h['hostname']] = {'status': 'disabled', 'services': []}

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(get_host_health, h['ip']): h['hostname'] for h in active}
        for future in as_completed(futures):
            name = futures[future]
            result = future.result()
            if types.get(name) == 'adhoc':
                result = _scrub_adhoc_etcd(result)
            host_health[name] = result

    return host_health


@app.route('/api/cluster-status')
def cluster_status_api():
    """API endpoint returning cluster status as JSON"""
    hosts = get_all_hosts()
    leadership = get_leadership_status()
    certificate_status = check_certificate_expiry()
    host_health = get_all_host_health(hosts)
    vip_status = get_cluster_vip_status(host_health)

    return jsonify({
        'hosts': hosts,
        'hostHealth': host_health,
        'leadership': leadership,
        'vipStatus': vip_status,
        'certificateStatus': certificate_status,
        'inferenceStatus': get_inference_status(),
        'respondingHostname': platform.node(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files"""
    import os
    from flask import Response
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(script_dir, 'static')
    
    # Get the response first
    response = send_from_directory(static_dir, filename)
    
    # Set correct MIME type for JavaScript files
    if filename.endswith('.js'):
        response.headers['Content-Type'] = 'application/javascript'
    
    return response

@app.route('/status')
def status_page():
    """Web page showing cluster-wide health status.
    All data is fetched client-side via /api/cluster-status — no server-side
    health checks here to avoid doing the work twice on every page load.
    """
    return render_template('status.html')

@app.route('/admin/utilization')
def utilization_page():
    """Embedded Utilization dashboard (fullscreen grafana kiosk)."""
    return render_template('utilization.html')


@app.route('/dashboard.html')
def utilization_page_legacy():
    """Pre-2026-06 location of the Utilization kiosk."""
    return redirect('/admin/utilization', code=301)


@app.route('/admin/inventory')
def inventory_page():
    """Hardware inventory and asset management page."""
    return render_template('inventory.html')


@app.route('/inventory')
def inventory_page_legacy():
    """Pre-2026-06 location — the page moved under /admin/ with the other
    signed-in pages (its /api/inventory* data endpoints did not move)."""
    return redirect('/admin/inventory', code=301)

@app.route('/admin/model-usage')
def model_usage_page():
    """Model usage stats page backed by cached PostgreSQL data."""
    since_days = request.args.get('since', 7, type=int)
    since_days = max(1, min(90, since_days))
    user_filter = request.args.get('user', '')
    model_filter = request.args.get('model', '')
    return render_template('admin-model-usage.html', since_days=since_days, user_filter=user_filter, model_filter=model_filter)


@app.route('/admin/model-usage/data')
def model_usage_data():
    """JSON endpoint for model usage data."""
    since_days = max(1, min(90, request.args.get('since', 7, type=int)))
    user_filter = request.args.get('user', '')
    model_filter = request.args.get('model', '')

    conn, since_dt, err = _usage_stats_cursor(since_days)
    if err:
        return jsonify({'error': err}), 500

    with conn:
        with conn.cursor() as cur:
            params = [since_dt]
            where = 'WHERE period_start >= %s'
            if user_filter:
                where += ' AND user_id = %s'
                params.append(user_filter)
            if model_filter:
                where += ' AND model = %s'
                params.append(model_filter)
            cur.execute(f'''
                SELECT
                    user_id,
                    model,
                    period_start::date AS date,
                    SUM(request_count) AS requests,
                    SUM(total_duration_ms)::float / NULLIF(SUM(request_count),0) AS avg_duration,
                    SUM(total_bytes_out) AS bytes
                FROM model_usage
                {where}
                GROUP BY user_id, model, period_start::date
                ORDER BY date DESC, user_id, model
            ''', params)
            rows = [
                {'user': str(r[0]), 'model': str(r[1]), 'date': str(r[2]),
                 'requests': r[3], 'avg_duration': r[4] or 0, 'bytes': r[5]}
                for r in cur.fetchall()
            ]
    return jsonify(rows)


def _usage_stats_cursor(since_days):
    # timedelta, not day-field arithmetic: replace(day=...) raises whenever
    # the window crosses a month boundary (since_days >= current day).
    since_dt = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since_dt = since_dt - timedelta(days=since_days)
    try:
        etcd = get_etcd_client()
        password_bytes = etcd.get('/cluster/config/usage_stats/db-password')[0]
        if password_bytes is None:
            return None, since_dt, 'password not found'
        password = password_bytes.decode()
    except Exception:
        return None, since_dt, 'etcd unavailable'
    try:
        conn = psycopg2.connect(host='10.0.0.100', database='usage_stats', user='usage_stats', password=password)
    except Exception as e:
        return None, since_dt, str(e)
    return conn, since_dt, None


# Single sources of truth: the etcd schema belongs to vm_manager (the
# writer); the admin group name to authentik_manager (which manages it).
from ycluster.utils.vm_manager import (VMS_PREFIX, VM_STATE_PREFIX,
                                       VM_DESIRED_PREFIX, vms_all)
from ycluster.utils.authentik_manager import ADMIN_GROUP


def _authentik_identity():
    """Identity from the forward-auth headers (set only by nginx on the
    external vhost; authentik joins groups with '|'). Internal callers
    (admin.xc, direct :12723) carry no headers and are root-equivalent
    operator space — treated as admin with no email."""
    email = request.headers.get('X-Authentik-Email') or None
    groups = [g for g in (request.headers.get('X-Authentik-Groups') or '').split('|') if g]
    is_admin = email is None or ADMIN_GROUP in groups
    return email, is_admin


@app.route('/admin/vm-schedule')
def vm_schedule_page():
    """Schedule VMs to be up: desired-state editor, converged by the
    per-host vm-reconciler."""
    return render_template('admin-vm-schedule.html')


@app.route('/admin/vm-schedule/data')
def vm_schedule_data():
    email, is_admin = _authentik_identity()
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503

    live = {}
    for value, metadata in client.get_prefix(VM_STATE_PREFIX):
        if not value:
            continue
        try:
            for inst in json.loads(value.decode()).get('instances', []):
                live[inst['name']] = inst.get('status')
        except Exception:
            continue

    desired_all = {}
    for value, metadata in client.get_prefix(VM_DESIRED_PREFIX):
        if not value:
            continue
        try:
            desired_all[metadata.key.decode()[len(VM_DESIRED_PREFIX):]] = \
                json.loads(value.decode())
        except Exception:
            continue

    rows = []
    for name, rec in vms_all().items():
        if not is_admin and rec.get('owner') != email:
            continue
        desired = desired_all.get(name)
        rows.append({
            'vm': name,
            'owner': rec.get('owner'),
            'host': rec.get('host'),
            'gpus': rec.get('gpus'),
            'status': live.get(name, 'unknown'),
            'mode': (desired or {}).get('mode', 'unmanaged'),
            'windows': (desired or {}).get('windows', []),
        })
    return jsonify({'rows': sorted(rows, key=lambda r: r['vm']),
                    'email': email, 'is_admin': is_admin})


def _parse_windows(windows):
    """Validate one-shot scheduling windows and normalize them to UTC ISO
    strings, sorted, with fully-elapsed windows dropped. Each window is
    {'start': <ISO datetime>, 'end': <ISO datetime>} with explicit
    timezones (the page sends UTC). Returns the normalized list, or None
    if anything is invalid."""
    if not isinstance(windows, list) or len(windows) > 50:
        return None
    now = datetime.now(UTC)
    out = []
    for w in windows:
        if not isinstance(w, dict):
            return None
        try:
            start = datetime.fromisoformat(str(w.get('start')))
            end = datetime.fromisoformat(str(w.get('end')))
        except (TypeError, ValueError):
            return None
        if start.tzinfo is None or end.tzinfo is None:
            return None
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if end <= start:
            return None
        if end <= now:
            continue
        out.append({'start': start.isoformat(timespec='seconds'),
                    'end': end.isoformat(timespec='seconds')})
    return sorted(out, key=lambda w: w['start'])


# Mutating endpoint by design (unlike the S2-removed admin mutations):
# externally it is only reachable through the forward-auth gate, so the
# identity headers are nginx-enforced; internal callers are operator space.
@app.route('/admin/vm-schedule/set', methods=['POST'])
def vm_schedule_set():
    email, is_admin = _authentik_identity()
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503

    data = request.get_json(silent=True) or {}
    name = data.get('vm', '')
    rec_raw = client.get(VMS_PREFIX + name)[0] if re.fullmatch(r'[a-z0-9-]+', name) else None
    if not rec_raw:
        return jsonify({'error': 'no such VM'}), 404
    rec = json.loads(rec_raw.decode())
    if not is_admin and rec.get('owner') != email:
        return jsonify({'error': 'not your VM'}), 403

    mode = data.get('mode')
    if mode == 'unmanaged':
        client.delete(VM_DESIRED_PREFIX + name)
        return jsonify({'ok': True, 'mode': 'unmanaged'})
    if mode not in ('on', 'off', 'schedule'):
        return jsonify({'error': 'mode must be on|off|schedule|unmanaged'}), 400
    windows = []
    if mode == 'schedule':
        windows = _parse_windows(data.get('windows', []))
        if windows is None:
            return jsonify({'error': 'invalid windows'}), 400

    desired = {
        'mode': mode,
        'windows': windows,
        'updated_by': email or 'internal',
        'updated_at': datetime.now(UTC).isoformat(timespec='seconds'),
    }
    client.put(VM_DESIRED_PREFIX + name, json.dumps(desired))
    return jsonify({'ok': True, 'mode': mode})


@app.route('/admin/users')
def users_page():
    """Account management against the cluster IdP (admin-only)."""
    return render_template('admin-users.html')


@app.route('/admin/users/data')
def users_data():
    _, is_admin = _authentik_identity()
    if not is_admin:
        return jsonify({'error': 'admin only'}), 403
    from ycluster.utils import authentik_manager
    try:
        return jsonify({'users': authentik_manager.users_data(),
                        'invitations': authentik_manager.invitations_data()})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# Mutating, admin-only (same forward-auth reasoning as vm_schedule_set);
# every action maps onto a `ycluster user` operation.
@app.route('/admin/users/action', methods=['POST'])
def users_action():
    email_id, is_admin = _authentik_identity()
    if not is_admin:
        return jsonify({'error': 'admin only'}), 403
    from ycluster.utils import authentik_manager
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    email = (data.get('email') or '').strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'error': 'invalid email'}), 400
    try:
        if action == 'invite':
            days = max(1, min(90, int(data.get('days') or 7)))
            url = authentik_manager.invite_user(email, data.get('name') or None, days)
            return jsonify({'ok': True, 'url': url})
        if action == 'uninvite':
            count = authentik_manager.revoke_invitation(email)
            return jsonify({'ok': True, 'message': f'revoked {count} invitation(s)'})
        if action == 'recovery':
            return jsonify({'ok': True, 'url': authentik_manager.recovery_link(email)})
        if action in ('admin_add', 'admin_remove'):
            msg = authentik_manager.set_admin(email, remove=(action == 'admin_remove'))
            return jsonify({'ok': True, 'message': msg})
        return jsonify({'error': 'unknown action'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/admin/vm-usage')
def vm_usage_page():
    """VM GPU-hour accounting: billable/tracked (events) vs observed (samples)."""
    since_days = max(1, min(90, request.args.get('since', 7, type=int)))
    return render_template('admin-vm-usage.html', since_days=since_days)


@app.route('/admin/vm-usage/data')
def vm_usage_data():
    """Per-VM GPU-hours in the window: billable + tracked from lifecycle
    events (interval assembly), observed from incus-state samples."""
    since_days = max(1, min(90, request.args.get('since', 7, type=int)))
    conn, since_dt, err = _usage_stats_cursor(since_days)
    if err:
        return jsonify({'error': err}), 500
    now = datetime.now(UTC)

    with conn:
        with conn.cursor() as cur:
            # Events inside the window, plus each VM's single latest event
            # before it — that one row decides whether an interval was open
            # (and with what gpus/billable) at the window start, so the scan
            # stays bounded as history grows.
            cur.execute('''SELECT DISTINCT ON (vm) ts, vm, event, owner, gpus, billable
                           FROM vm_events WHERE ts < %s
                           ORDER BY vm, ts DESC''', [since_dt])
            pre_events = cur.fetchall()
            cur.execute('''SELECT ts, vm, event, owner, gpus, billable
                           FROM vm_events WHERE ts >= %s
                           ORDER BY vm, ts''', [since_dt])
            events = pre_events + cur.fetchall()
            cur.execute('''
                SELECT vm, MAX(COALESCE(owner, '')),
                       SUM(CASE WHEN state = 'Running' THEN gpus * interval_s ELSE 0 END)::float / 3600,
                       COUNT(*) FILTER (WHERE state = 'Running')
                FROM vm_samples WHERE ts >= %s
                GROUP BY vm
            ''', [since_dt])
            sampled = {r[0]: {'owner': r[1], 'observed': r[2] or 0.0,
                              'samples': r[3]} for r in cur.fetchall()}
            cur.execute('''SELECT DISTINCT ON (vm) vm, state
                           FROM vm_samples ORDER BY vm, ts DESC''')
            last_state = dict(cur.fetchall())

    # Assemble running intervals per VM from the event stream (each VM's
    # pre-window event sorts before its window events, and the state machine
    # is per-VM, so the combined list needs no global re-sort). launch/start
    # open an interval (restart keeps one open across the gap), stop/destroy
    # close it; an interval still open accrues until now. Time is clipped to
    # the window; billable is a property of the opening event.
    def accrue(st, end):
        start, ev_gpus, ev_billable = st['open']
        lo, hi = max(start, since_dt), min(end, now)
        if hi > lo:
            hours = (hi - lo).total_seconds() / 3600 * (ev_gpus or 0)
            st['tracked'] += hours
            if ev_billable:
                st['billable'] += hours

    per_vm = {}
    for ts, vm, event, owner, gpus, billable in events:
        st = per_vm.setdefault(vm, {'owner': owner, 'open': None,
                                    'billable': 0.0, 'tracked': 0.0})
        if owner:
            st['owner'] = owner
        if event in ('launch', 'start', 'restart'):
            if st['open'] is None:
                st['open'] = (ts, gpus, billable)
        elif event in ('stop', 'destroy'):
            if st['open'] is not None:
                accrue(st, ts)
                st['open'] = None
    for st in per_vm.values():
        if st['open'] is not None:
            accrue(st, now)

    rows = []
    for vm in sorted(set(per_vm) | set(sampled)):
        ev = per_vm.get(vm, {})
        sm = sampled.get(vm, {})
        tracked = round(ev.get('tracked', 0.0), 4)
        observed = round(sm.get('observed', 0.0), 4)
        # Is the VM on the clock right now, and on whose dime: an open
        # event interval says billable/unbilled; running per the latest
        # sample with no open interval is untracked runtime.
        if ev.get('open') is not None:
            current = 'billable' if ev['open'][2] else 'unbilled'
        elif last_state.get(vm) == 'Running':
            current = 'untracked'
        else:
            current = None
        rows.append({
            'vm': vm,
            'owner': ev.get('owner') or sm.get('owner') or '',
            'billable_gpu_hours': round(ev.get('billable', 0.0), 4),
            'tracked_gpu_hours': tracked,
            'observed_gpu_hours': observed,
            'samples': sm.get('samples', 0),
            'current': current,
            # Sampling granularity is one interval; flag drift beyond that
            # plus 10% slack.
            'drift': abs(observed - tracked) > max(0.1, tracked * 0.1),
        })
    return jsonify(rows)


@app.route('/admin/model-usage/options')
def model_usage_options():
    """JSON endpoint returning distinct users and models for filter dropdowns."""
    since_days = max(1, min(90, request.args.get('since', 7, type=int)))

    conn, since_dt, err = _usage_stats_cursor(since_days)
    if err:
        return jsonify({'error': err}), 500

    with conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT DISTINCT user_id FROM model_usage WHERE period_start >= %s ORDER BY user_id''', [since_dt])
            users = [str(r[0]) for r in cur.fetchall()]
            cur.execute('''SELECT DISTINCT model FROM model_usage WHERE period_start >= %s ORDER BY model''', [since_dt])
            models = [str(r[0]) for r in cur.fetchall()]
    return jsonify({'users': users, 'models': models})


if __name__ == '__main__':
    # Wait for etcd to be available — only on storage (s*) nodes, which are the
    # only ones that talk to etcd. Non-storage admin-api instances must start
    # (and serve local /metrics) without etcd, since etcd is firewalled to s*.
    if is_etcd_node():
        while True:
            try:
                client = get_etcd_client()
                print("Connected to etcd successfully")
                break
            except Exception as e:
                print(f"Waiting for etcd: {e}")
                time.sleep(5)
    else:
        print("Non-storage node: skipping etcd wait (etcd access is core-only)")

    # waitress: production WSGI server, single process with a thread pool —
    # same concurrency model the in-process allocation_lock assumes.
    from waitress import serve
    serve(app, host='0.0.0.0', port=12723, threads=8)
