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
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, cast
from urllib.parse import urlsplit

from flask import Flask, request, jsonify, redirect, render_template, send_from_directory
import os
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

from app_gpu_blocks import (gpu_block_record, invalid_windows,
                            resolve_gpu_block, validate_gpu_block)
import app_autoinstall
import app_inventory
import app_project_db
import app_projects
import project_routes
import settings_routes
import app_schedule
import app_wg
from app_schedule import schedule_gpu_count, validate_schedule_mode
from app_allocation import (determine_ip_from_hostname, determine_type_from_mac,
                            get_next_hostname, get_or_create_allocation)
from app_helpers import (_add_health_service, _assemble_intervals,
                         _can_edit_vm, _gateway_vip_health,
                         _parse_windows, _required_service_health,
                         _standby_service_health, _storage_vip_health,
                         _usage_row)
from health_common import (check_port_open, check_service_status,
                           check_tang_service)
from ycluster.common.etcd_utils import get_etcd_client

AUTOINSTALL_USER_DATA_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'user-data.j2')
MACOS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'macos-bootstrap.sh.j2')
NAS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'nas-bootstrap.sh.j2')
NVIDIA_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'nvidia-bootstrap.sh.j2')
WG_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'wg-bootstrap.sh.j2')
WG_MACOS_BOOTSTRAP_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'wg-macos-bootstrap.sh.j2')

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

# Inference backend registry, owned by local-ai-proxy. Prometheus uses it
# via /api/prometheus/targets/vllm to discover vLLM metrics endpoints.
INFERENCE_MODELS_PREFIX = '/cluster/config/inference/models/'
VLLM_RUNTIME_CACHE_TTL = 60  # seconds
_vllm_runtime_cache: dict[str, tuple[float, str | None]] = {}
_vllm_runtime_lock = threading.Lock()

# Cluster VIPs (follow the storage leader via keepalived). Used to render
# bootstrap script callbacks that must land on the leader regardless of
# which managed node served the bootstrap page.
STORAGE_VIP = '10.0.0.100'
GATEWAY_VIP = '10.0.0.254'

# Core nodes are derived dynamically from etcd allocations (see
# get_core_nodes()) so adding a new storage node doesn't require a code
# change. The inventory plugin treats any s\d+ hostname as a core node;
# we mirror that by filtering on type == 'storage'.

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
# is in app_allocation.py.

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
        allocation, created = get_or_create_allocation(
            mac_address, node_type=node_type)

        return jsonify({
            'hostname': allocation['hostname'],
            'type': allocation['type'],
            'ip': allocation['ip'],
            'amt_ip': allocation['amt_ip'],
            'mac': mac_address,
            'existing': not created
        })
    except Exception as e:
        return jsonify({'error': f'Allocation failed: {str(e)}'}), 500

@app.route('/api/wg/register', methods=['POST'])
def wg_register():
    """Allocate a node and register a WireGuard peer in one atomic call."""
    from ycluster.utils import wg_config

    data = request.get_json(silent=True) or {}
    mac, node_type, pubkey, error = app_wg.validate_register_input(data)
    if error:
        return jsonify({'error': error}), 400

    allocation, peer, failure = app_wg.register_peer(
        get_or_create_allocation, wg_config.register_peer,
        mac, node_type, pubkey)
    if failure:
        return jsonify({'error': failure[1]}), failure[0]

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

def _node_hosts_entries(client):
    """Generate hosts entries for cluster nodes registered in etcd."""
    entries = []
    for value, _metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if not value:
            continue
        try:
            allocation = json.loads(value.decode())
            hostname = allocation['hostname']
            ip = allocation['ip']

            # Skip AMT hostnames registered as nodes - they get correct
            # entries auto-generated from the base node below
            if hostname.endswith('a') and determine_ip_from_hostname(hostname):
                continue

            entries.append(f"{ip} {hostname} {hostname}.xc")

            # Add AMT hostname entry if this is a regular node (not already AMT)
            if not hostname.endswith('a'):
                amt_hostname = f"{hostname}a"
                amt_ip = determine_ip_from_hostname(amt_hostname)
                if amt_ip:
                    entries.append(f"{amt_ip} {amt_hostname} {amt_hostname}.xc")
        except Exception:
            pass
    return entries


def _frontend_hosts_entries(client):
    """Generate entries for frontend nodes (separate etcd prefix).

    Frontend nodes sit outside the cluster subnet — emit their reachable
    address so the cluster can resolve/ping/ssh them by name. Only
    IP-registered nodes can go in a hosts file; hostname-registered ones
    already resolve via DNS.
    """
    entries = []
    for value, _metadata in client.get_prefix("/cluster/nodes/frontend/"):
        if not value:
            continue
        try:
            node = json.loads(value.decode())
            name = node.get('name')
            ip = node.get('ip')
            if name and ip:
                entries.append(f"{ip} {name} {name}.xc")
        except Exception:
            pass
    return entries


@app.route('/api/hosts')
def get_hosts():
    """Generate hosts file format from etcd allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return f"# etcd connection failed: {str(e)}\n", 503

    hosts_entries = _node_hosts_entries(client)
    hosts_entries.extend(_frontend_hosts_entries(client))

    # Add service aliases that point to storage VIP
    hosts_entries.extend([
        f"{STORAGE_VIP} registry.xc",
        f"{STORAGE_VIP} admin.xc",
        f"{STORAGE_VIP} inference.xc",
        f"{STORAGE_VIP} auth.xc",
        f"{STORAGE_VIP} forgejo.xc",
        f"{STORAGE_VIP} vikunja.xc",
    ])

    # Forgejo advertises git.<domain> for SSH clone URLs when external SSH
    # is enabled. Resolve it to the storage leader internally so cluster
    # clients don't hairpin through the public frontend. auth.<domain> gets
    # the same treatment: the IdP discovery URL is the public hostname.
    # Vikunja's external hostname is resolved internally for the same reason.
    domain = _configured_external_domain(client)
    if domain:
        hosts_entries.extend([
            f"{STORAGE_VIP} git.{domain}",
            f"{STORAGE_VIP} auth.{domain}",
            f"{STORAGE_VIP} tasks.{domain}",
        ])

    if hosts_entries:
        return '\n'.join(sorted(hosts_entries)) + '\n', 200, {'Content-Type': 'text/plain'}
    return "# No static hosts configured yet\n", 200, {'Content-Type': 'text/plain'}


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

def _dns_query_details():
    """Query the local resolver for this host and return (working, message)."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = ['127.0.0.1']
        resolver.timeout = 3
        resolver.lifetime = 5

        local_hostname = platform.node()
        answer = resolver.resolve(local_hostname, 'A')
        if answer:
            resolved_ips = [str(rdata) for rdata in answer]
            return True, (f"Local DNS server responding "
                          f"({local_hostname} -> {', '.join(resolved_ips)})")
        return False, f"Local DNS query for {local_hostname} returned no results"
    except dns.resolver.Timeout:
        return False, "Local DNS query timeout"
    except dns.resolver.NXDOMAIN:
        return False, "Local DNS query: domain not found"
    except dns.resolver.NoAnswer:
        return False, "Local DNS query: no answer"
    except Exception as e:
        return False, f"Local DNS query error: {str(e)}"


def check_dns_status():
    """Check DNS (dnsmasq) service and functionality"""
    try:
        service_running = check_service_status('dnsmasq')
        dns_working, dns_details = _dns_query_details()

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
        return {'status': 'error', 'details': f"DNS check failed: {str(e)}"}


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

        cert_pem = cert_value.decode()
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        expires_at = cert.not_valid_after
        now = datetime.now(UTC).replace(tzinfo=None)
        days_until_expiry = (expires_at - now).days

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
    import ntplib   # lazy: only this health check needs it
    from ycluster.utils.clock_skew import classify_clock_offset

    # NTP server to check against (VIP)
    ntp_server = '10.0.0.254'

    try:
        # Create NTP client
        client = ntplib.NTPClient()

        # Make NTP request (this is a lightweight UDP request)
        response = client.request(ntp_server, version=3, timeout=2)

        # Get offset in milliseconds
        offset_ms = response.offset * 1000

        # Thresholds are looser off storage nodes (see clock_skew module).
        status = classify_clock_offset(offset_ms, get_current_node_type())

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

def _registry_probe_status():
    """Probe the Docker registry /v2/ endpoint.

    Returns (healthy, api_version, error) — tries localhost then the
    storage VIP, stopping at the first 200 response.
    """
    test_urls = [f'http://localhost:5000/v2/', f'http://{STORAGE_VIP}:5000/v2/']
    error = None
    for url in test_urls:
        try:
            response = requests.get(url, timeout=5)
        except requests.exceptions.Timeout:
            error = 'Registry health check timeout'
        except requests.exceptions.ConnectionError:
            error = 'Registry connection failed'
        except Exception as e:
            error = f'Registry health check failed: {str(e)}'
        else:
            if response.status_code == 200:
                version = response.headers.get('Docker-Distribution-Api-Version', 'unknown')
                return True, version, None
            error = f'Registry health check returned HTTP {response.status_code}'
    return False, None, error


def _registry_leader_status(running, port_open, healthy, version, error):
    """Determine status/message on the storage leader."""
    if running and port_open and healthy:
        return 'healthy', f'Registry running and healthy (API version {version})'
    if running and port_open:
        return 'degraded', f'Registry running but health check failed: {error}'
    if running:
        return 'unhealthy', f'Registry running but port not accessible: {error}'
    return 'unhealthy', 'Registry not running'


def check_docker_registry():
    """Check Docker registry status and functionality"""
    try:
        service_running = check_service_status('docker-registry')
        port_open = check_port_open('localhost', 5000)
        healthy, version, error = (False, None, None)
        if port_open:
            healthy, version, error = _registry_probe_status()

        is_storage_lead = is_storage_leader()
        running = service_running  # container check is a no-op (always False)

        if is_storage_lead:
            status, message = _registry_leader_status(running, port_open, healthy, version, error)
        elif running or port_open:
            status, message = 'unhealthy', 'Split-brain: Registry running on non-leader'
        else:
            status, message = 'standby', 'Registry not required (not storage leader)'

        return {
            'status': status,
            'details': {
                'service_active': service_running,
                'container_running': False,
                'port_open': port_open,
                'health_check_passed': healthy,
                'api_version': version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': error
            }
        }
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Registry check failed: {str(e)}'}
        }


def _secrets_mount_details():
    """Return (mounted, findmnt details) for /secrets."""
    result = subprocess.run(['mountpoint', '-q', '/secrets'],
                            capture_output=True, text=True, timeout=5)
    mount_details = None
    if result.returncode == 0:
        try:
            mount_result = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS', '/secrets'],
                capture_output=True, text=True, timeout=5)
            if mount_result.returncode == 0:
                mount_details = mount_result.stdout.strip()
        except Exception:
            pass
    return result.returncode == 0, mount_details


def _secrets_access(is_mounted):
    """Return (accessible, error) for /secrets; mounted-but-root-only is OK."""
    try:
        if os.path.isdir('/secrets'):
            os.listdir('/secrets')
            return True, None
        return False, '/secrets directory does not exist'
    except PermissionError:
        return is_mounted, None
    except Exception as e:
        return False, f'Error accessing /secrets: {str(e)}'


def check_secrets_mount():
    """Check if /secrets is mounted"""
    try:
        is_mounted, mount_details = _secrets_mount_details()
        secrets_accessible, secrets_error = _secrets_access(is_mounted)

        if is_mounted and secrets_accessible:
            status = 'healthy'
            message = 'Secrets volume mounted and accessible'
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
        return {'status': 'error',
                'details': {'message': f'Secrets mount check failed: {str(e)}'}}

def _open_webui_health_probe():
    """Probe the Open-WebUI health endpoint.

    Returns (healthy, version, error).
    """
    try:
        response = requests.get('http://localhost:8380/health', timeout=5)
    except requests.exceptions.Timeout:
        return False, None, 'Open-WebUI health check timeout'
    except requests.exceptions.ConnectionError:
        return False, None, 'Open-WebUI connection failed'
    except Exception as e:
        return False, None, f'Open-WebUI health check failed: {str(e)}'

    if response.status_code != 200:
        return False, None, f'Open-WebUI health check returned HTTP {response.status_code}'
    try:
        version = response.json().get('version', 'unknown')
    except Exception:
        version = 'unknown'
    return True, version, None


def _open_webui_leader_status(running, port_open, healthy, version, error):
    """Determine status/message on the storage leader."""
    if running and port_open and healthy:
        return 'healthy', f'Open-WebUI running and healthy (version {version})'
    if running and port_open:
        return 'degraded', f'Open-WebUI running but health check failed: {error}'
    if running:
        return 'unhealthy', f'Open-WebUI service running but port not accessible: {error}'
    return 'unhealthy', 'Open-WebUI service not running'


def check_open_webui():
    """Check Open-WebUI service status and functionality"""
    try:
        running = check_service_status('open-webui')
        port_open = check_port_open('localhost', 8380)
        healthy, version, error = (False, None, None)
        if port_open:
            healthy, version, error = _open_webui_health_probe()
        is_storage_lead = is_storage_leader()

        if is_storage_lead:
            status, message = _open_webui_leader_status(running, port_open, healthy, version, error)
        elif running or port_open:
            status, message = 'unhealthy', 'Split-brain: Open-WebUI running on non-leader'
        else:
            status, message = 'standby', 'Open-WebUI not required (not storage leader)'

        return {
            'status': status,
            'details': {
                'service_active': running,
                'port_open': port_open,
                'health_check_passed': healthy,
                'version': version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': error
            }
        }
    except Exception as e:
        return {'status': 'error', 'details': {'message': f'Open-WebUI check failed: {str(e)}'}}



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

@app.route('/api/inventory/asset/<hostname>', methods=['PUT', 'POST'])
@validated_hostname
def inventory_set_asset(hostname):
    """Set asset metadata fields for a node (admin only).

    Re-applied from pre-S2 with authentik admin gating: forward-auth sets
    X-Authentik-Email / X-Authentik-Groups on the external vhost; internal
    callers carry no headers and are root-equivalent. Non-admins get 403.
    Managers (view-only) also get 403 — they can browse the inventory but
    cannot edit asset fields.
    """
    if not _authentik_identity().is_admin:
        return jsonify({'error': 'admin only'}), 403
    try:
        from ycluster.utils.inventory import put_asset
        data = request.get_json(force=True)
        if not data:
            return jsonify({'error': 'No JSON body'}), 400
        allowed = {'vendor', 'purchased_at', 'warranty_expires',
                   'cost', 'cost_currency', 'location', 'notes'}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if not filtered:
            return jsonify({'error': 'No valid fields in request'}), 400
        result = put_asset(hostname, filtered)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory')
def inventory_list():
    """Return full inventory (hardware + asset) for all nodes (internal only).

    Response shape: ``{"inventory": [...], "is_admin": bool, "is_manager":
    bool, "email": str}``. ``is_admin`` lets the page render edit widgets;
    ``is_manager`` is exposed for future UI use (today the page already
    treats ``!is_admin`` as read-only, which covers managers too).
    """
    id = _authentik_identity()
    try:
        from ycluster.utils.inventory import list_all
        rows = list_all()
        out = []
        for r in rows:
            alloc = r['allocation'] or {}
            out.append({
                'hostname': r.get('hostname') or alloc.get('hostname'),
                'type': alloc.get('type'),
                'ip': alloc.get('ip'),
                'hardware': r['hardware'],
                'asset': r['asset'],
            })
        out.sort(key=lambda x: x['hostname'] or '')
        return jsonify({'inventory': out,
                        'is_admin': id.is_admin,
                        'is_manager': id.is_manager,
                        'email': id.email})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory/export.csv')
def inventory_export_csv():
    """Export full inventory as CSV (internal only)"""
    try:
        from ycluster.utils.inventory import list_all
        rows = list_all()
        buf = app_inventory.export_csv(rows)
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

def _mac_from_dhcp_leases(client, client_ip):
    """Look up a MAC address from DHCP leases in etcd."""
    for value, _metadata in client.get_prefix('/cluster/dhcp/leases/'):
        if not value:
            continue
        try:
            lease_data = json.loads(value.decode())
            if lease_data.get('ip') == client_ip:
                return lease_data.get('mac')
        except json.JSONDecodeError:
            # Skip non-JSON entries in the dhcp prefix
            continue
    return None


def _mac_from_neighbor_table(client_ip):
    """Look up a MAC address via `ip --json neigh show`, validating format."""
    print("fallback to neighbor table", client_ip, file=sys.stderr)
    try:
        result = subprocess.run(['ip', '--json', 'neigh', 'show', client_ip],
                                capture_output=True, text=True, timeout=5)
        neighbors = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"neighbor table JSON parse error: {e}", file=sys.stderr)
        print(f"Raw stdout was: {result.stdout or ''}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"neighbor table lookup failed for {client_ip}: {e}",
              file=sys.stderr)
        return None

    for neighbor in neighbors:
        print(f"neighbor: {neighbor}", file=sys.stderr)
        if neighbor.get('dst') != client_ip or 'lladdr' not in neighbor:
            continue
        mac = neighbor['lladdr']
        if isinstance(mac, str) and len(mac) == 17 and mac.count(':') == 5:
            return mac
        print("MAC validation failed", file=sys.stderr)
    return None


def get_mac_from_ip(client_ip):
    """
    Look up MAC address from IP address using DHCP leases in etcd or neighbor table.
    Return value is non-normalized MAC address (xx:xx:xx:xx:xx:xx) or None if not found.
    """
    if not client_ip:
        return None

    client = get_etcd_client()
    mac = _mac_from_dhcp_leases(client, client_ip)
    if mac:
        return mac
    return _mac_from_neighbor_table(client_ip)


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
    allocation_data, _ = get_or_create_allocation(mac_address)
    
    # Use allocation data
    node_type = allocation_data['type']
    hostname = allocation_data['hostname']
    ip_address = allocation_data['ip']
    amt_ip_address = allocation_data['amt_ip']
    
    # Get interface configuration for this node type (fallback to 'unknown')
    interfaces = NODE_TYPE_INTERFACES.get(node_type, NODE_TYPE_INTERFACES['unknown'])
    
    ssh_key_content = app_autoinstall.ssh_key('/opt/bootstrap-files/ansible_ssh_key.pub')
    ubuntu_password = app_autoinstall.ubuntu_password()
    proxy_url = f'http://{GATEWAY_VIP}:3128'

    rendered_content = app_autoinstall.render_user_data(
        Template(open(AUTOINSTALL_USER_DATA_TEMPLATE).read()),
        allocation_data, interfaces, proxy_url, GATEWAY_VIP,
        ssh_key_content, ubuntu_password)
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
    api_server = f"http://{STORAGE_VIP}"
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
    # nas/nvidia) target the storage VIP so callbacks (/api/allocate,
    # /api/wg/register) land on the storage leader regardless of which
    # managed node served the bootstrap page.
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
        api_server = f"http://{STORAGE_VIP}"

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


def _service_health_value(status):
    """Map a service status string to a numeric Prometheus value."""
    return {'healthy': 0, 'degraded': 1, 'unhealthy': 2,
            'standby': 3}.get(status, 2)


def _node_metric(node, name, value):
    return f'ycluster_{name}{{node="{node}"}} {value}'


def _service_metrics(node, health_data):
    """Render per-service health and ceph-status metric lines."""
    metrics = []
    for service, details in health_data.get('services', {}).items():
        value = _service_health_value(details.get('status'))
        metrics.append(f'ycluster_service_health{{node="{node}",service="{service}"}} {value}')
        if service == 'ceph' and isinstance(details.get('details'), dict):
            ok = details['details'] == 'HEALTH_OK'
            metrics.append(f'ycluster_ceph_health{{node="{node}"}} {1 if ok else 0}')
    return metrics


def _leader_metrics(node, health_data):
    storage_leader = 1 if health_data.get('storage_leader', False) else 0
    dhcp_leader = 1 if health_data.get('dhcp_leader', False) else 0
    return [
        _node_metric(node, 'storage_leader', storage_leader),
        _node_metric(node, 'dhcp_leader', dhcp_leader),
    ]


def _vip_metrics(node):
    vip_status = check_vip_status()
    return [
        f'ycluster_vip_active{{node="{node}",vip="gateway"}} '
        f'{1 if vip_status["gateway_vip"]["active"] else 0}',
        f'ycluster_vip_active{{node="{node}",vip="storage"}} '
        f'{1 if vip_status["storage_vip"]["active"] else 0}',
    ]


def _cert_metrics(node):
    cert_status = check_certificate_expiry()
    days = cert_status.get('details', {}).get('days_until_expiry')
    if days is not None:
        return [_node_metric(node, 'ycluster_certificate_days_until_expiry', days)]
    return []


def _prometheus_health_metrics(health_data):
    """Render node/service/leader/vip/cert/drain metric lines."""
    node = platform.node()
    overall_value = 1 if health_data['overall'] == 'healthy' else 0
    metrics = [_node_metric(node, 'node_healthy', overall_value)]
    metrics.extend(_service_metrics(node, health_data))
    metrics.extend(_leader_metrics(node, health_data))
    metrics.extend(_vip_metrics(node))
    metrics.extend(_cert_metrics(node))
    drained = 1 if health_data.get('drained', False) else 0
    metrics.append(_node_metric(node, 'node_drained', drained))
    return metrics


@app.route('/metrics')
def prometheus_metrics():
    """Prometheus metrics endpoint"""
    try:
        health_data = get_comprehensive_health()
        metrics = _prometheus_health_metrics(health_data)
        response = '\n'.join(metrics) + '\n'
        return response, 200, {'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'}
    except Exception:
        error_response = f'ycluster_metrics_error{{node="{platform.node()}"}} 1\n'
        return error_response, 500, {'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'}








def _etcd_health():
    if not is_etcd_node():
        return {'status': 'not_applicable', 'details': {'reason': 'admin-api uses etcd only on storage nodes'}}
    try:
        client = get_etcd_client()
        client.get('/test')
        return {'status': 'healthy', 'details': 'connected'}
    except Exception as e:
        return {'status': 'unhealthy', 'details': str(e)}


def _ceph_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_ceph_status()


def _postgres_health(is_storage_lead):
    running = check_service_status('postgresql@16-main')
    port = check_port_open('localhost', 5432)
    if is_storage_lead:
        return _required_service_health(running, port, 'postgresql', 'storage leader')
    return _standby_service_health(running, port, 'not storage leader')


def _qdrant_health(is_storage_lead):
    running = check_service_status('qdrant')
    port = check_port_open('localhost', 6333)
    if is_storage_lead:
        return _required_service_health(running, port, 'qdrant', 'storage leader')
    return _standby_service_health(running, port, 'not storage leader')


def _leader_election_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    running = check_service_status('storage-leader-election')
    return {
        'status': 'healthy' if running else 'unhealthy',
        'details': {'service_active': running}
    }


def _dhcp_health(is_dhcp_lead):
    port = check_port_open('localhost', 8067)
    if is_dhcp_lead:
        return {
            'status': 'healthy' if port else 'unhealthy',
            'details': {
                'health_port_open': port,
                'required': True,
                'reason': 'dhcp leader'
            }
        }
    running = port
    return {
        'status': 'unhealthy' if running else 'standby',
        'details': {
            'health_port_open': port,
            'required': False,
            'reason': 'split-brain: dhcp running on non-leader' if running else 'not dhcp leader'
        }
    }


def _squid_health():
    running = check_service_status('squid')
    port = check_port_open('localhost', 3128)
    functional = False
    error = None

    if running and port:
        try:
            response = requests.get(
                'http://localhost:12723/api/ping',
                proxies={'http': 'http://localhost:3128'},
                timeout=5
            )
            if response.status_code in [200, 503]:
                functional = True
            else:
                error = f'HTTP {response.status_code}'
        except requests.exceptions.ProxyError as e:
            error = f'Proxy error: {str(e)}'
        except requests.exceptions.Timeout:
            error = 'Proxy timeout'
        except Exception as e:
            error = f'Proxy test failed: {str(e)}'

    healthy = running and port and functional
    return {
        'status': 'healthy' if healthy else 'unhealthy',
        'details': {
            'service_active': running,
            'port_open': port,
            'proxy_functional': functional,
            'error': error
        }
    }


def _ntp_health():
    running = check_service_status('ntp') or check_service_status('chrony')
    return {
        'status': 'healthy' if running else 'unhealthy',
        'details': {'service_active': running}
    }


def _keepalived_health():
    current_hostname = platform.node()
    if is_etcd_node() and current_hostname in get_core_nodes():
        running = check_service_status('keepalived')
        return {
            'status': 'healthy' if running else 'unhealthy',
            'details': {'service_active': running}
        }

    running = check_service_status('keepalived')
    if running:
        return {
            'status': 'unhealthy',
            'details': {
                'service_active': running,
                'reason': 'split-brain: keepalived running on non-core node'
            }
        }
    return {
        'status': 'standby',
        'details': {
            'service_active': running,
            'reason': 'not a core node'
        }
    }


def get_comprehensive_health():
    """Get comprehensive health data (extracted from health() function)"""
    health_status = {
        'overall': 'healthy',
        'services': {}
    }
    current_node_type = get_current_node_type()
    is_storage_node = current_node_type == 'storage'

    _add_health_service(health_status, 'etcd', _etcd_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'ceph', _ceph_health(is_storage_node), ('unhealthy', 'error', 'standby', 'unknown'), ())

    is_storage_lead = is_storage_leader()
    _add_health_service(health_status, 'postgresql', _postgres_health(is_storage_lead), ('unhealthy',), ())
    _add_health_service(health_status, 'qdrant', _qdrant_health(is_storage_lead), ('unhealthy',), ())
    _add_health_service(health_status, 'storage_leader_election', _leader_election_health(is_storage_node), ('unhealthy',), ())
    _add_health_service(health_status, 'dhcp', _dhcp_health(is_dhcp_leader()), ('unhealthy',), ())
    _add_health_service(health_status, 'dns', check_dns_status(), ('unhealthy',), ())
    _add_health_service(health_status, 'squid', _squid_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'ntp', _ntp_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'tls_certificate', check_certificate_expiry(), ('expired', 'critical'), ('warning',))
    _add_health_service(health_status, 'clock_skew', check_clock_skew(), ('critical', 'error'), ('warning',))
    _add_health_service(health_status, 'docker_daemon', _docker_daemon_health(is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'docker_registry', check_docker_registry(), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'tang', _tang_health(is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'secrets_mount', _secrets_mount_health(is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'open_webui', check_open_webui(), ('unhealthy', 'error'), ('degraded',))

    vip_health = check_vip_status()
    _add_health_service(health_status, 'gateway_vip', _gateway_vip_health(vip_health), (), ())
    _add_health_service(health_status, 'storage_vip', _storage_vip_health(vip_health), (), ())
    _add_health_service(health_status, 'keepalived', _keepalived_health(), ('unhealthy',), ())

    health_status['storage_leader'] = is_storage_lead
    health_status['dhcp_leader'] = is_dhcp_leader()
    health_status['drained'] = is_node_drained()

    return health_status


def _docker_daemon_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_docker_daemon()


def _tang_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_tang_service()


def _secrets_mount_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_secrets_mount()





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

def get_host_health(host_ip, timeout=5):
    """Get health status from a specific host"""
    # Every managed host (storage, compute, nvidia, nas, macos) serves its
    # own /api/health on the unified 12724 health-api port. admin-api (on
    # 12723) is separate and storage-only — never queried for health.
    # Heartbeat flaps on slow/flaky nodes motivated the diagnostic fields
    # below (elapsed_ms, exception, phase) — without them a timeout vs
    # connect-refused vs DNS failure all collapse to "Connection failed".
    import time
    start = time.monotonic()
    def _elapsed():
        return round((time.monotonic() - start) * 1000, 1)
    try:
        response = requests.get(f"http://{host_ip}:12724/api/health", timeout=timeout)
        if response.status_code in [200, 503]:
            # Both 200 (healthy) and 503 (unhealthy) contain valid health data
            result = response.json()
            result['_fetch_elapsed_ms'] = _elapsed()
            return result
        else:
            return {'overall': 'error', 'services': {},
                    'error': f'HTTP {response.status_code}',
                    'exception': 'HTTPError',
                    'elapsed_ms': _elapsed()}
    # ConnectTimeout is a subclass of both Timeout and ConnectionError, so it
    # must be checked before either parent — otherwise it lands in the wrong
    # bucket and we lose the connect-vs-read distinction.
    except requests.exceptions.ConnectTimeout as e:
        return {'overall': 'unreachable', 'phase': 'connect', 'services': {},
                'error': 'Connect timeout', 'exception': type(e).__name__,
                'elapsed_ms': _elapsed()}
    except requests.exceptions.ReadTimeout as e:
        return {'overall': 'timeout', 'phase': 'read', 'services': {},
                'error': 'Read timeout', 'exception': type(e).__name__,
                'elapsed_ms': _elapsed()}
    except requests.exceptions.ConnectionError as e:
        return {'overall': 'unreachable', 'phase': 'connect', 'services': {},
                'error': str(e) or 'Connection failed',
                'exception': type(e).__name__,
                'elapsed_ms': _elapsed()}
    except requests.exceptions.Timeout as e:
        return {'overall': 'timeout', 'phase': 'unknown', 'services': {},
                'error': 'Request timeout', 'exception': type(e).__name__,
                'elapsed_ms': _elapsed()}
    except Exception as e:
        return {'overall': 'error', 'services': {},
                'error': str(e), 'exception': type(e).__name__,
                'elapsed_ms': _elapsed()}

def _vip_status_for_ip(ip):
    """Run `ip addr` and return one VIP detail dict."""
    details = {'ip': ip, 'active': False, 'master': None, 'interface': None}
    try:
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', ip],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            interfaces = json.loads(result.stdout)
            if interfaces:
                details['active'] = True
                details['master'] = platform.node()
                details['interface'] = interfaces[0].get('ifname')
    except json.JSONDecodeError as e:
        details['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        details['error'] = str(e)
    return details


def check_vip_status():
    """Check VIP status using keepalived and ip commands"""
    vip_status = {
        'gateway_vip': _vip_status_for_ip('10.0.0.254'),
        'storage_vip': _vip_status_for_ip('10.0.0.100'),
    }
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
# The vLLM job is discovered from the inference registry rather than a host
# list: one host can run several backends on different ports, and only some
# of them are vLLM. Runtime identity comes from backend /v1/models responses.
def _inference_backend_urls(client) -> list[str]:
    """Return the distinct backend URLs registered in the inference tree."""
    urls = set()
    for value, metadata in client.get_prefix(INFERENCE_MODELS_PREFIX):
        key = metadata.key.decode() if isinstance(metadata.key, bytes) else str(metadata.key)
        try:
            record = json.loads(value.decode() if isinstance(value, (bytes, bytearray)) else value)
            backends = record.get('backends') or []
        except (AttributeError, TypeError, ValueError) as exc:
            app.logger.warning('inference discovery: skipping malformed %s: %s', key, exc)
            continue
        for entry in backends:
            api_base = entry.get('api_base') if isinstance(entry, dict) else None
            if isinstance(api_base, str) and api_base:
                urls.add(api_base.rstrip('/'))
    return sorted(urls)


def _backend_runtime(api_base: str) -> str | None:
    """Return the engine a backend reports via /v1/models, briefly cached.

    vLLM reports owned_by="vllm"; sglang, mlx_lm and llama.cpp report their
    own names. None means the probe failed or the response was unusable, in
    which case the backend is not offered for scraping.
    """
    now = time.monotonic()
    with _vllm_runtime_lock:
        cached = _vllm_runtime_cache.get(api_base)
    if cached and cached[0] > now:
        return cached[1]

    runtime = None
    try:
        response = requests.get(f'{api_base}/v1/models', timeout=3)
        if response.status_code == 200:
            owners = {
                str(model.get('owned_by', '')).lower()
                for model in response.json().get('data') or []
                if isinstance(model, dict)
            }
            if 'vllm' in owners:
                runtime = 'vllm'
            elif owners:
                runtime = sorted(owners)[0]
    except (AttributeError, TypeError, ValueError, requests.RequestException):
        runtime = None

    with _vllm_runtime_lock:
        _vllm_runtime_cache[api_base] = (now + VLLM_RUNTIME_CACHE_TTL, runtime)
    return runtime


def _runtime_sd_targets(runtime: str) -> list[dict]:
    """Return HTTP SD entries for registered backends serving one engine."""
    urls = _inference_backend_urls(get_etcd_client())
    with ThreadPoolExecutor(max_workers=8) as pool:
        runtimes = dict(zip(urls, pool.map(_backend_runtime, urls)))

    targets = []
    seen = set()
    for api_base in urls:
        if runtimes[api_base] != runtime:
            continue
        parsed = urlsplit(api_base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            continue
        if parsed.netloc in seen:
            continue
        seen.add(parsed.netloc)
        targets.append({
            'targets': [parsed.netloc],
            'labels': {'backend': f'{parsed.scheme}://{parsed.netloc}'},
        })
    return targets


def _serve_runtime_targets(runtime: str):
    try:
        return jsonify(_runtime_sd_targets(runtime))
    except Exception as exc:
        return jsonify({'error': f'inference discovery failed: {exc}'}), 503


@app.route('/api/prometheus/targets/vllm')
def prometheus_targets_vllm():
    """Prometheus HTTP SD list of inference backends that report as vLLM."""
    return _serve_runtime_targets('vllm')


@app.route('/api/prometheus/targets/sglang')
def prometheus_targets_sglang():
    """Prometheus HTTP SD list of inference backends that report as sglang."""
    return _serve_runtime_targets('sglang')


# Each host job maps to a port and a predicate (applied to get_all_hosts()
# output) that decides which hosts should be scraped. The admin API is
# scraped by Prometheus itself, so these endpoints can answer target queries
# dynamically on every refresh interval — disabling a host via
# `ycluster cluster disable` immediately removes it from the target list
# without re-running ansible.
PROMETHEUS_JOB_SPECS: dict[str, dict[str, int | Callable[[dict], bool]]] = {
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
        predicate = cast(Callable[[dict], bool], spec['predicate'])
        port = cast(int, spec['port'])
        if not predicate(host):
            continue
        targets.append({
            'targets': [f"{host['ip']}:{port}"],
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


def _disabled_health(hosts):
    """Seed the health map with disabled-host placeholders."""
    return {h['hostname']: {'status': 'disabled', 'services': []}
            for h in hosts if h.get('disabled', False)}


HEALTH_API_HOST_TYPES = frozenset(
    {'storage', 'compute', 'nvidia', 'nas', 'macos'})


def get_all_host_health(hosts):
    """Fetch health for non-disabled hosts that serve health-api."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    types = {h['hostname']: h.get('type') for h in hosts}
    host_health = _disabled_health(hosts)
    active = [h for h in hosts
              if h['hostname'] not in host_health
              and h.get('type') in HEALTH_API_HOST_TYPES]

    with ThreadPoolExecutor(max_workers=16) as executor:
        # Use hostname, not IP — etcd's IP field can be stale (e.g. after a
        # DHCP renewal moves the host to a different /24) but cluster DNS
        # (dnsmasq) is authoritative and always reflects the current address.
        futures = {executor.submit(get_host_health, h['hostname']): h['hostname'] for h in active}
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

@app.route('/api/frontend/nat-rules')
def frontend_nat_rules_api():
    """Read-only exposure list for the frontend nat-reconciler agent.

    Scoped: returns only the fields the frontend needs to render its
    nftables ruleset. Auth is enforced at the nginx layer (mTLS with a
    frontend-scoped client CA) — see docs/design/vm-frontend-nat.md.
    Defence in depth: refuse the request unless nginx confirms a
    verified client cert was presented (fail-closed if the header is
    absent, so a misconfigured nginx cannot silently expose the data).
    """
    if request.headers.get('X-SSL-Client-Verify') != 'SUCCESS':
        return jsonify({'error': 'client certificate required'}), 401
    from ycluster.utils import frontend_nat
    try:
        return jsonify({'exposures': frontend_nat.list_exposures()})
    except Exception as e:
        app.logger.error('frontend nat-rules: %s', e)
        return jsonify({'error': 'unavailable'}), 503


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


@app.route('/admin/resources')
def resources_page():
    """Per-compute-machine view of models and VMs/containers."""
    return render_template('admin-resources.html')


@app.route('/admin/resources/data')
def resources_data():
    """JSON for the Resources page: per compute host, the models it serves
    (from inference backends) and the VMs/containers registered to it (with
    GPU counts). VMs not currently scheduled to run are flagged so the page
    can highlight them."""
    id = _authentik_identity()
    from ycluster.utils.inventory import list_all as inventory_list_all
    from ycluster.utils import vm_manager as vmm

    hosts = get_all_hosts()
    inv = {r['hostname']: r for r in inventory_list_all()}

    try:
        registry = vmm.vms_all()
        desired_all = vmm.desired_all()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503

    now = datetime.now(UTC)
    compute_types = {'compute', 'nvidia', 'macos'}

    # Group VMs by host
    vms_by_host = {}
    for name, rec in registry.items():
        host = rec.get('host')
        if not host:
            continue
        vms_by_host.setdefault(host, []).append(name)

    # Managers (non-admins) are project-scoped: they only see VMs owned by
    # accounts in a project they're authorized for (none by default). Admins
    # and plain users keep the existing roster behavior. Per-owner project
    # lookups are cached so we don't hit the IdP per VM.
    managed_projects = None
    owner_project = {}
    if id.is_manager and not id.is_admin:
        from ycluster.utils import authentik_manager
        managed_projects = set(authentik_manager.manager_projects(id.email)
                               if id.email else [])

        def _owner_visible(owner):
            if owner not in owner_project:
                try:
                    project = (authentik_manager.user_project(owner)
                               if owner else '')
                except Exception:
                    # No IdP account (orphan VM owner) or transient IdP
                    # failure: the owner can't be attributed to a project,
                    # so fail closed — out of scope for a manager instead of
                    # 500ing the whole page.
                    project = ''
                owner_project[owner] = project
            return owner_project[owner] in managed_projects
    else:
        def _owner_visible(owner):
            return True

    # GPUs currently scheduled/committed per host: the same desired-state
    # commitments the schedule page and admission control use (VMs plus
    # admin GPU reservations/blocks). This is the "used" half of the
    # used/total badge on each host card.
    commitments = vmm.combine_gpu_commitments(
        vmm.gpu_commitments(registry, desired_all, now),
        vmm.gpu_block_commitments(vmm.gpu_blocks_all(), now))
    gpus_used = {host: sum(c['gpus'] for c in commits)
                 for host, commits in commitments.items()}

    # Build per-host data
    result_hosts = []
    for h in hosts:
        if h['type'] not in compute_types:
            continue
        hostname = h['hostname']
        inv_rec = inv.get(hostname, {})
        hardware = inv_rec.get('hardware') or {}
        gpus = hardware.get('gpus') or []

        host_vms = []
        for vm_name in vms_by_host.get(hostname, []):
            rec = registry.get(vm_name, {})
            if not _owner_visible(rec.get('owner')):
                continue
            desired = desired_all.get(vm_name) or {}
            mode = desired.get('mode', 'unmanaged')
            # A VM is "scheduled now" if desired_on says it should be up.
            scheduled_now = vmm.desired_on(desired, now) if desired else False
            # "Currently scheduled" GPU count: what the VM should run with
            # per its desired record (defaults to all registered GPUs), as
            # opposed to the registered max it was launched with.
            sched_gpus = vmm.desired_gpus(desired, rec.get('gpus') or 0)
            host_vms.append({
                'name': vm_name,
                'owner': rec.get('owner'),
                'gpus': rec.get('gpus', 0),
                'sched_gpus': sched_gpus,
                'cpu': rec.get('cpu'),
                'mem': rec.get('mem'),
                'type': rec.get('type', 'vm'),
                'state': rec.get('state', 'unknown'),
                'mode': mode,
                'windows': desired.get('windows', []),
                'scheduled_now': scheduled_now,
            })

        result_hosts.append({
            'hostname': hostname,
            'type': h['type'],
            'ip': h['ip'],
            'disabled': h.get('disabled', False),
            'hardware': hardware,
            'gpu_count': len(gpus),
            'gpus_used': gpus_used.get(hostname, 0),
            'gpus': gpus,
            'vms': host_vms,
        })

    return jsonify({
        'hosts': result_hosts,
        'inference': get_inference_status(),
        'is_admin': id.is_admin,
        'is_manager': id.is_manager,
        'email': id.email,
    })


@app.route('/admin/model-usage')
def model_usage_page():
    """Model usage stats page backed by cached PostgreSQL data. Admins only —
    managers (and plain users) are denied, and the nav link is hidden from
    them (see nav.js)."""
    if not _authentik_identity().is_admin:
        return 'forbidden', 403
    since_days = request.args.get('since', 7, type=int)
    since_days = max(1, min(90, since_days))
    user_filter = request.args.get('user', '')
    model_filter = request.args.get('model', '')
    return render_template('admin-model-usage.html', since_days=since_days, user_filter=user_filter, model_filter=model_filter)


@app.route('/admin/model-usage/data')
def model_usage_data():
    """JSON endpoint for model usage data. Admin-only: managers must not see
    cross-project inference usage."""
    if not _authentik_identity().is_admin:
        return jsonify({'error': 'admin only'}), 403
    since_days = max(1, min(90, request.args.get('since', 7, type=int)))
    user_filter = request.args.get('user', '')
    model_filter = request.args.get('model', '')

    conn, since_dt, err = _usage_stats_cursor(_since_days_dt(since_days))
    if err:
        return jsonify({'error': err}), 500
    assert conn is not None

    with conn:
        with conn.cursor() as cur:
            params: list = [since_dt]
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


def _since_days_dt(days):
    # timedelta, not day-field arithmetic: replace(day=...) raises whenever
    # the window crosses a month boundary (since_days >= current day).
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days)


def _usage_stats_cursor(since_dt):
    try:
        etcd = get_etcd_client()
        password_bytes = etcd.get('/cluster/config/usage_stats/db-password')[0]
        if password_bytes is None:
            return None, since_dt, 'password not found'
        password = password_bytes.decode()
    except Exception:
        return None, since_dt, 'etcd unavailable'
    try:
        import psycopg2   # lazy: only the usage-stats (postgres) endpoints need it
        conn = psycopg2.connect(host='10.0.0.100', database='usage_stats', user='usage_stats', password=password)
    except Exception as e:
        return None, since_dt, str(e)
    return conn, since_dt, None


# Single sources of truth: the etcd schema + data-access layer belong to
# vm_manager (the writer/owner of these prefixes) — the VM endpoints below
# go through its domain functions, never the raw etcd client; the admin
# group name to authentik_manager (which manages it).
import ipaddress
from collections import namedtuple

from ycluster.common import apikeys
from ycluster.utils import vm_manager as vmm
from ycluster.utils.authentik_manager import ADMIN_GROUP, MANAGER_GROUP


Identity = namedtuple('Identity', ['uid', 'email', 'is_admin', 'is_manager'])


def _authentik_identity():
    """Identity from the forward-auth headers (set only by nginx on the
    external vhost; authentik joins groups with '|'). Internal callers
    (admin.xc, direct :12723) carry no headers and are root-equivalent
    operator space — treated as admin with no email.

    Identity signal priority: ``X-Authentik-UID`` (always present for any
    authenticated user, including OAuth-sourced accounts without a
    verified email) is the primary "is this authenticated?" check.
    ``X-Authentik-Email`` is optional metadata — it can be missing for
    external-source users and shouldn't drive authorization.

    Belt-and-suspenders: admin-api binds 0.0.0.0:12723 today, so a remote
    cluster node could in principle hit the Flask app directly without
    going through nginx. Two tiers:

    1. The "no headers → admin" operator-space shortcut requires
       ``remote_addr`` to be EXACTLY ``127.0.0.1`` AND no UID set. A
       nginx upstream from the public frp vhost (listen
       ``127.0.0.2:443``) may arrive on ``127.0.0.2``; a public request
       that has somehow bypassed forward-auth would land here headerless,
       and we don't want that treated as admin. An OAuth-authenticated
       user without a verified email still has a UID set, so they're
       correctly routed through the group check (not the shortcut).

    2. The forward-auth group check requires loopback (``127.0.0.0/8``)
       so a remote cluster node can't smuggle admin headers either.

    Returns an ``Identity(uid, email, is_admin, is_manager)``: admins
    implicitly pass the manager check (a manager is "an admin minus the
    mutations")."""
    email = request.headers.get('X-Authentik-Email') or None
    uid = request.headers.get('X-Authentik-UID') or None
    groups = [g for g in (request.headers.get('X-Authentik-Groups') or '').split('|') if g]
    try:
        remote = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        remote = None
    is_loopback = remote is not None and remote.is_loopback
    is_local = request.remote_addr == "127.0.0.1"
    # The headerless shortcut is the dangerous path — require the caller
    # to be definitively on the same host (not just any loopback) AND
    # have no UID at all (a UID means an authenticated user is on the
    # other end, even if email is missing).
    is_admin = (is_local and uid is None) \
        or (is_loopback and ADMIN_GROUP in groups)
    # Manager: same loopback guard as admin (a remote cluster node
    # smuggling MANAGER_GROUP headers must not be treated as manager),
    # plus admins implicitly pass.
    is_manager = is_admin \
        or (is_loopback and uid is not None and MANAGER_GROUP in groups)
    return Identity(uid, email, is_admin, is_manager)


def _configured_external_domain(client):
    """Return the canonical external `<domain>` from etcd, or '' when unset."""
    try:
        value, _ = client.get('/cluster/https/domain')
        return value.decode().strip() if value else ''
    except Exception:
        return ''


def _external_domain():
    """External `<domain>` suffix used to build cross-subdomain app links
    (chat.<domain>, zulip.<domain>, admin.<domain>). Prefers the canonical
    value in etcd (/cluster/https/domain); falls back to deriving the domain
    from the request host so a dev/force-local install that hasn't seeded
    the domain still renders links. The landing page is served at the bare
    <domain> (previously app.<domain>), so the fallback accepts both shapes."""
    try:
        domain = _configured_external_domain(get_etcd_client())
        if domain:
            return domain
    except Exception:
        pass
    host = (request.host or '').split(':', 1)[0]
    if host.startswith('app.'):
        return host.split('.', 1)[1]
    if host and host != 'localhost':
        return host
    return None


def _gpu_admission_error(name, rec, mode, windows, gpus=None):
    """Admission control for a proposed desired state: None if the VM's
    chosen GPU count (`gpus`, defaulting to its registered count) fits
    its host's pool against the commitments already accepted for the
    other VMs (and admin GPU blocks) there, else a rejection message.
    Fails open when the host hasn't reported a pool size yet (sampler
    predates the gpu_pool field) — better to accept than to brick
    scheduling during a rollout."""
    host = rec.get('host')
    if gpus is None:
        gpus = rec.get('gpus') or 0
    if rec.get('type') == 'container' or not host or not gpus \
            or mode not in ('on', 'schedule'):
        return None
    snap = vmm.state_get(host)
    pool = snap.get('gpu_pool') if snap else None
    if not isinstance(pool, int):
        return None
    now = datetime.now(UTC)
    registry = vmm.vms_all()
    registry.pop(name, None)
    commits = vmm.combine_gpu_commitments(
        vmm.gpu_commitments(registry, vmm.desired_all(), now),
        vmm.gpu_block_commitments(vmm.gpu_blocks_all(), now)).get(host, [])
    if mode == 'on':
        ranges = [(now, datetime.max.replace(tzinfo=UTC))]
    else:
        ranges = [(datetime.fromisoformat(w['start']),
                   datetime.fromisoformat(w['end'])) for w in windows]
    for lo, hi in ranges:
        worst = vmm.gpu_conflict(commits, gpus, pool, lo, hi)
        if worst:
            p, free, active = worst
            held = ', '.join(f"{c['vm']} ({c['gpus']})" for c in active)
            when = ('now' if p == now
                    else p.strftime('%Y-%m-%d %H:%M UTC'))
            return (f"needs {gpus} GPU(s) on {host} but only "
                    f"{max(free, 0)} uncommitted at {when} "
                    f"(pool {pool}; held by {held})")
    return None


def _gpu_block_admission_error(host, gpus, mode, windows, exclude=None):
    """Admission control for a proposed GPU block: None if `gpus` on
    `host` fits the pool against the committed VMs and other blocks, else
    a rejection message. Mirrors `_gpu_admission_error` (same fail-open
    without a reported pool). `exclude` is a block_id to ignore when
    editing an existing block, so it doesn't count against itself."""
    if not host or not gpus or mode not in ('on', 'schedule'):
        return None
    snap = vmm.state_get(host)
    pool = snap.get('gpu_pool') if snap else None
    if not isinstance(pool, int):
        return None
    now = datetime.now(UTC)
    blocks = vmm.gpu_blocks_all()
    if exclude:
        blocks.pop(exclude, None)
    commits = vmm.combine_gpu_commitments(
        vmm.gpu_commitments(vmm.vms_all(), vmm.desired_all(), now),
        vmm.gpu_block_commitments(blocks, now)).get(host, [])
    if mode == 'on':
        ranges = [(now, datetime.max.replace(tzinfo=UTC))]
    else:
        ranges = [(datetime.fromisoformat(w['start']),
                   datetime.fromisoformat(w['end'])) for w in windows]
    for lo, hi in ranges:
        worst = vmm.gpu_conflict(commits, gpus, pool, lo, hi)
        if worst:
            p, free, active = worst
            held = ', '.join(f"{c['vm']} ({c['gpus']})" for c in active)
            when = ('now' if p == now
                    else p.strftime('%Y-%m-%d %H:%M UTC'))
            return (f"block needs {gpus} GPU(s) on {host} but only "
                    f"{max(free, 0)} uncommitted at {when} "
                    f"(pool {pool}; held by {held})")
    return None




@app.route('/admin/nav-identity')
def nav_identity():
    """Role signal for nav.js so it can show/hide admin-only links (e.g.
    Model Usage) for managers versus admins. Kept tiny because it's hit on
    every admin page load."""
    id = _authentik_identity()
    return jsonify({'is_admin': id.is_admin, 'is_manager': id.is_manager})


@app.route('/')
def app_landing():
    """Root of <domain>: a landing page linking the user-facing apps.

    Served behind forward-auth (like the other self-service paths), so the
    page can gate the Admin link on the caller's identity. Individual apps
    live on sibling subdomains of the primary domain, which we resolve from
    etcd (or the request host). The root's own pages (VM Schedule, Settings,
    API Keys) are not listed — the app nav bar already links them."""
    domain = _external_domain()
    id = _authentik_identity()
    apps = []
    if domain:
        apps += [
            {'name': 'Open WebUI', 'url': f'https://chat.{domain}/',
             'logo': '/static/open-webui.svg',
             'desc': 'Chat, documents, and local models'},
            {'name': 'Zulip', 'url': f'https://zulip.{domain}/',
             'logo': '/static/zulip.svg',
             'desc': 'Team chat and discussions'},
            {'name': 'Forgejo', 'url': f'https://git.{domain}/',
             'logo': '/static/forgejo.svg',
             'desc': 'Git repositories, reviews, and Actions'},
            {'name': 'Vikunja', 'url': f'https://tasks.{domain}/',
             'logo': '/static/vikunja.svg',
             'desc': 'Tasks, projects, and boards'},
        ]
        if id.is_admin or id.is_manager:
            apps.append({'name': 'YCluster Admin',
                         'url': f'https://admin.{domain}/',
                         'logo': '/static/ycluster-favicon.svg',
                         'desc': 'Inventory, utilization, users, monitoring'})
    return render_template('app-landing.html', domain=domain or '', apps=apps)


@app.route('/admin/vm-schedule')
def vm_schedule_page():
    """Schedule VMs to be up: desired-state editor, converged by the
    per-host vm-reconciler."""
    return render_template('admin-vm-schedule.html')


@app.route('/admin/vm-schedule/data')
def vm_schedule_data():
    id = _authentik_identity()
    try:
        states = vmm.state_all()
        desired_all = vmm.desired_all()
        issues = vmm.issues_all()
        graces = vmm.grace_all()
        registry = vmm.vms_all()
        blocks = vmm.gpu_blocks_all()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503

    from ycluster.utils import authentik_manager
    from app_schedule import live_state, schedule_hosts, schedule_rows
    live, pools = live_state(states)
    now = datetime.now(UTC)
    commitments = vmm.combine_gpu_commitments(
        vmm.gpu_commitments(registry, desired_all, now),
        vmm.gpu_block_commitments(blocks, now))
    visible, shown = app_schedule.owner_filters(id, authentik_manager)

    rows = schedule_rows(id, registry, desired_all, issues, graces, live,
                         commitments, pools, now, _can_edit_vm,
                         _parse_windows, vmm, authentik_manager)
    hosts = schedule_hosts(id, rows, blocks, commitments, pools, shown)
    block_list = schedule_blocks(id, blocks)
    return jsonify({'rows': sorted(rows, key=lambda r: r['vm']),
                    'hosts': hosts,
                    'blocks': sorted(block_list, key=lambda b: b['block']),
                    'email': id.email,
                    'is_admin': id.is_admin,
                    'is_manager': id.is_manager,
                    'grace_seconds': vmm.SCHEDULED_STOP_GRACE_S,
                    'ssh': {'frp_host': _frp_public_host(),
                            'jump_user': vmm.BASTION_JUMP_USER,
                            'vm_user': vmm.VM_GUEST_USER}})


def schedule_blocks(id, blocks):
    """Render the admin GPU block roster for managers."""
    if not id.is_manager:
        return []
    return [{
        'block': block_id,
        'host': b.get('host'),
        'gpus': b.get('gpus'),
        'mode': b.get('mode'),
        'reason': b.get('reason'),
        'created_by': b.get('created_by'),
        'created_at': b.get('created_at'),
        'updated_at': b.get('updated_at'),
        'windows': b.get('windows', []),
    } for block_id, b in blocks.items()]


def _frp_public_host():
    """Public host clients reach the frp frontend on — the host part of the
    frp tunnel's `remote_addr` in /cluster/nodes/frp/config. Returns only the
    host, never the secret. None if unset/unparseable."""
    try:
        raw, _ = get_etcd_client().get('/cluster/nodes/frp/config')
        if not raw:
            return None
        remote = json.loads(raw.decode()).get('remote_addr', '')
        return remote.rsplit(':', 1)[0] or None
    except Exception:
        return None




# Mutating endpoint by design (unlike the S2-removed admin mutations):
# externally it is only reachable through the forward-auth gate, so the
# identity headers are nginx-enforced; internal callers are operator space.
# A manager can edit a VM they own (same as a plain user); they can't
# edit other users' VMs — the ownership check below covers both cases.
@app.route('/admin/vm-schedule/set', methods=['POST'])
def vm_schedule_set():
    id = _authentik_identity()
    data = request.get_json(silent=True) or {}
    name = data.get('vm', '')
    if not re.fullmatch(r'[a-z0-9-]+', name):
        return jsonify({'error': 'no such VM'}), 404
    try:
        rec = vmm.vm_get(name)
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503
    if not rec:
        return jsonify({'error': 'no such VM'}), 404
    if not id.is_admin and rec.get('owner') != id.email:
        return jsonify({'error': 'not your VM'}), 403

    mode = data.get('mode')
    error = validate_schedule_mode(id, name, mode, vmm)
    if error:
        return jsonify(error[0]), error[1]

    # Windows are preserved across every mode (including unmanaged) so a
    # user can flip on/off/unmanaged as a temporary override and switch
    # back to 'schedule' with their plan intact — the reconciler only acts
    # on windows under 'schedule', and never touches an 'unmanaged' VM at
    # all. Require valid windows only for 'schedule' (the live mode);
    # tolerate junk as dormant data for the others. Clearing the plan is a
    # deliberate edit (remove the windows), not a side effect of a mode flip.
    windows = _parse_windows(data.get('windows', []))
    if mode == 'schedule' and windows is None:
        return jsonify({'error': 'invalid windows'}), 400
    if windows is None:
        windows = []

    # GPU count to run this desired state with — how many of the VM's
    # allocated GPUs the scheduler should hold/attach. Absent (backwards
    # compat) means all of them (the registered record). Bounded to the
    # registered count so a save can never over-allocate past the VM.
    gpus, error = schedule_gpu_count(rec, data.get('gpus'))
    if error:
        return jsonify(error[0]), error[1]

    # Admission control self-gates to the modes that newly commit GPUs
    # against the host pool ('on' = always-held, 'schedule' = windows);
    # 'off'/'unmanaged' return None (no new commitment).
    conflict = _gpu_admission_error(name, rec, mode, windows, gpus=gpus)
    if conflict:
        return jsonify({'error': conflict}), 409

    vmm.desired_set(name, {
        'mode': mode,
        'gpus': gpus,
        'windows': windows,
        # updated_by is for audit: prefer email (human-readable), fall
        # back to the UID (always present for an authenticated user),
        # and finally 'internal' for the operator-space shortcut. A user
        # authenticated via OAuth without a verified email will be
        # recorded by UID — readable via the audit log mapping table.
        'updated_by': id.email or id.uid or 'internal',
        'updated_at': datetime.now(UTC).isoformat(timespec='seconds'),
    })
    return jsonify({'ok': True, 'mode': mode})


# Mutating, forward-auth/owner gated like vm_schedule_set. Stamps an
# `immediate` grace marker so the next reconciler tick stops the VM without
# waiting out the remaining grace (the guest has effectively been warned).
# A manager can stop-now their own VM (same as a plain user).
@app.route('/admin/vm-schedule/stop-now', methods=['POST'])
def vm_schedule_stop_now():
    id = _authentik_identity()
    data = request.get_json(silent=True) or {}
    name = data.get('vm', '')
    if not re.fullmatch(r'[a-z0-9-]+', name):
        return jsonify({'error': 'no such VM'}), 404
    try:
        rec = vmm.vm_get(name)
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503
    if not rec:
        return jsonify({'error': 'no such VM'}), 404
    if not id.is_admin and rec.get('owner') != id.email:
        return jsonify({'error': 'not your VM'}), 403

    # Only meaningful as a pending-stop bypass: the schedule must currently
    # want the VM OFF. (If it wants ON, there's nothing to stop.)
    desired = vmm.desired_get(name)
    if not desired or desired.get('mode') == 'unmanaged' \
            or vmm.desired_on(desired, datetime.now(UTC)):
        return jsonify({'error': 'no pending stop for this VM'}), 409

    vmm.grace_set(name, {
        'warned_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'immediate': True,
    })
    return jsonify({'ok': True})


# Admin-only GPU blocks ("reservations"): a schedule entry with no backing
# VM. A block holds `gpus` on `host` for the pool admission control, so the
# scheduler refuses VMs that would overcommit past it, without provisioning
# anything. Only admins may create/edit/delete; `block` (optional) edits an
# existing block in place, otherwise a new one is created with a
# server-generated id.
@app.route('/admin/vm-schedule/gpu-block', methods=['POST'])
def gpu_block_upsert():
    id = _authentik_identity()
    if not id.is_admin:
        return jsonify({'error': 'only admins can manage GPU blocks'}), 403
    data = request.get_json(silent=True) or {}

    fields, error = validate_gpu_block(data)
    if error:
        return jsonify(error[0]), error[1]
    block_id = fields['block_id']

    block_id, cur, error = resolve_gpu_block(block_id, vmm)
    if error:
        return jsonify(error[0]), error[1]

    windows = _parse_windows(data.get('windows', []))
    # A 'schedule' block with no live window holds nothing, which makes the
    # reservation meaningless; require at least one. 'on' blocks carry none.
    error = invalid_windows(fields['mode'], windows)
    if error:
        return jsonify(error[0]), error[1]

    conflict = _gpu_block_admission_error(
        fields['host'], fields['gpus'], fields['mode'], windows,
        exclude=block_id)
    if conflict:
        return jsonify({'error': conflict}), 409

    rec = gpu_block_record(
        id, cur, fields['host'], fields['gpus'], fields['mode'], windows,
        fields['reason'])
    try:
        vmm.gpu_block_set(block_id, rec)
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503
    return jsonify({'ok': True, 'block': block_id,
                    'mode': fields['mode']})


@app.route('/admin/vm-schedule/gpu-block/delete', methods=['POST'])
def gpu_block_remove():
    id = _authentik_identity()
    if not id.is_admin:
        return jsonify({'error': 'only admins can manage GPU blocks'}), 403
    data = request.get_json(silent=True) or {}
    block_id = data.get('block', '')
    if not re.fullmatch(r'[a-z0-9-]+', block_id):
        return jsonify({'error': 'no such block'}), 404
    try:
        if not vmm.gpu_block_get(block_id):
            return jsonify({'error': 'no such block'}), 404
        vmm.gpu_block_delete(block_id)
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {e}'}), 503
    return jsonify({'ok': True})


@app.route('/admin/users')
def users_page():
    """Account management against the cluster IdP. Admins get the full page;
    managers get the roster with project-scoped note-edit / invite /
    recovery controls (see docs/design/manager-project-scoped-users.md)."""
    return render_template('admin-users.html')


@app.route('/admin/users/data')
def users_data():
    # Admins (full) and managers (project-scoped) see the roster; plain
    # users get 403.
    id = _authentik_identity()
    if not id.is_manager:
        return jsonify({'error': 'admin only'}), 403
    from ycluster.utils import authentik_manager
    try:
        users = authentik_manager.users_data()
        if id.is_admin:
            allowed = None
            manager_projects = []
            invitations = authentik_manager.invitations_data()
        else:
            manager_projects = (authentik_manager.manager_projects(id.email)
                                if id.email else [])
            allowed = set(manager_projects)
            invitations = authentik_manager.invitations_data(projects=allowed)
            # Project-scope the roster: managers only see accounts that
            # belong to a project they're authorized for (none by default).
            users = [u for u in users if u.get('project_name') in allowed]
        for u in users:
            # The roster is already scoped to what this caller may manage,
            # so every visible row is manageable.
            u['can_manage'] = True
        return jsonify({'users': users,
                        'invitations': invitations,
                        # Tell the page the caller's capabilities: admins
                        # get the full menu; managers get project-scoped
                        # note-edit / invite / recovery. manager_projects
                        # drives the invite project selector for managers.
                        'is_admin': id.is_admin,
                        'manager_projects': manager_projects})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/admin/users/action', methods=['POST'])
def users_action():
    id = _authentik_identity()
    if not id.is_manager:
        try:
            app.logger.info('admin-user-audit %s', json.dumps({
                'action': 'unauthorized',
                'target': '',
                'actor_uid': id.uid,
                'actor_email': id.email,
                'remote_addr': request.remote_addr,
                'status': '403',
            }))
        except Exception:
            app.logger.exception('admin-user-audit failed')
        return jsonify({'error': 'admin only'}), 403
    from app_users import (admin_user_action, authorized_projects,
                           edit_user, invite_user, recovery_user,
                           set_active_user)
    from ycluster.utils import authentik_manager
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    email = (data.get('email') or '').strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'error': 'invalid email'}), 400

    def _audit(status):
        try:
            app.logger.info('admin-user-audit %s', json.dumps({
                'action': action,
                'target': email,
                'actor_uid': id.uid,
                'actor_email': id.email,
                'remote_addr': request.remote_addr,
                'status': status,
            }))
        except Exception:
            app.logger.exception('admin-user-audit failed')

    admin = id.is_admin
    try:
        projects = authorized_projects(id, admin, authentik_manager)
        if action in ('uninvite', 'admin_add', 'admin_remove',
                      'manager_add', 'manager_remove'):
            if not admin:
                return jsonify({'error': 'admin only'}), 403
            payload, status = admin_user_action(
                action, email, authentik_manager)
        elif action in ('activate', 'deactivate'):
            payload, status = set_active_user(
                email, action == 'activate', projects, authentik_manager,
                admin=admin)
        elif action == 'invite':
            payload, status = invite_user(
                email, data, projects, authentik_manager, admin=admin)
        elif action == 'recovery':
            payload, status = recovery_user(
                email, projects, authentik_manager, admin=admin)
        elif action == 'edit':
            payload, status = edit_user(
                email, data, projects, authentik_manager, admin=admin)
        else:
            payload, status = {'error': 'unknown action'}, 400
        _audit(status if status else (payload or {}).get('error') or 'ok')
        return jsonify(payload), status
    except Exception as e:
        _audit(f'exception: {e}')
        return jsonify({'error': str(e)}), 400


project_routes.register(app, _authentik_identity)


settings_routes.register(sys.modules[__name__])


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
