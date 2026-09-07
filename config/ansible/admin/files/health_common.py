"""Shared state and node-role helpers for the lite health API."""

import json
import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import requests

from flask import g

ETCD_PREFIX = '/cluster/nodes'
GATEWAY_VIP = "10.0.0.254"
STORAGE_VIP = "10.0.0.100"

# Static per-process facts (don't change after boot).
# Storage nodes (= s*) are the only ones that can talk to etcd —
# etcd is firewalled to s* and non-storage health-api instances
# (compute/macos/nvidia/nas/adhoc) must not build an etcd client.
# `compute` in inventory is c* plus nvidia as a child group; the rest
# of the lite health-api surface treats them all the same.
_HOSTNAME = socket.gethostname()
_IS_STORAGE_NODE = _HOSTNAME.startswith('s')
_IS_ETCD_NODE = _IS_STORAGE_NODE


# ---- per-request cache ---------------------------------------------------
#
# flask.g is request-scoped and cleaned up automatically between
# requests, so it can't leak stale state across requests (unlike a
# module-level dict + manual reset that future routes might forget to
# call). It is also implicitly thread-safe if Flask is ever run with
# threaded=True.

def init_request_cache():
    g._cache = {}


def _cache():
    return g._cache


# ---- check helpers (duplicated from app.py to stay import-light) --------

def check_service_status(service_name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def check_port_open(host, port, timeout=3):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


@dataclass(frozen=True)
class HealthProbeContext:
    """Injectable I/O seam for health checks.

    Callables let tests provide complete fake probe data without reaching
    into private module state.
    """

    is_etcd_node: bool
    is_storage_node: bool
    etcd_client: Callable[[], Any]
    service_status: Callable[[str], bool]
    port_open: Callable[..., bool]
    http_get: Callable[..., Any]
    storage_leader: Callable[[], bool]
    dhcp_leader: Callable[[], bool]
    node_drained: Callable[[], bool]
    core_node: Callable[[], bool]


# ---- etcd helpers --------------------------------------------------------
#
# Each of these creates an etcd client on first call within a request.
# On nodes where etcd is firewalled or down, each connection attempt
# takes ~2s to time out, and with 7 separate calls the health endpoint
# would take ~14s — longer than the system test's 10s curl timeout.
# The per-request cache ensures the etcd client is created at most once
# per request and leader/drain results are reused.

def _get_etcd_client():
    cache = _cache()
    if 'etcd_client' not in cache:
        from ycluster.common.etcd_utils import get_etcd_client
        try:
            cache['etcd_client'] = get_etcd_client()
        except Exception:
            cache['etcd_client'] = None
    return cache['etcd_client']


def is_storage_leader():
    """Check if this node is the current storage leader"""
    cache = _cache()
    if 'is_storage_leader' in cache:
        return cache['is_storage_leader']
    if not _IS_ETCD_NODE:
        cache['is_storage_leader'] = False
        return False
    try:
        client = _get_etcd_client()
        if client is None:
            cache['is_storage_leader'] = False
            return False
        result = client.get('/cluster/leader/app')
        if result[0]:
            cache['is_storage_leader'] = result[0].decode() == _HOSTNAME
            return cache['is_storage_leader']
        cache['is_storage_leader'] = False
        return False
    except:
        cache['is_storage_leader'] = False
        return False


def is_dhcp_leader():
    """Check if this node is the current DHCP leader"""
    cache = _cache()
    if 'is_dhcp_leader' in cache:
        return cache['is_dhcp_leader']
    if not _IS_ETCD_NODE:
        cache['is_dhcp_leader'] = False
        return False
    try:
        client = _get_etcd_client()
        if client is None:
            cache['is_dhcp_leader'] = False
            return False
        result = client.get('/cluster/leader/dhcp')
        if result[0]:
            cache['is_dhcp_leader'] = result[0].decode() == _HOSTNAME
            return cache['is_dhcp_leader']
        cache['is_dhcp_leader'] = False
        return False
    except:
        cache['is_dhcp_leader'] = False
        return False


def is_node_drained():
    """Check if this node is drained"""
    cache = _cache()
    if 'is_node_drained' in cache:
        return cache['is_node_drained']
    if not _IS_ETCD_NODE:
        cache['is_node_drained'] = False
        return False
    try:
        client = _get_etcd_client()
        if client is None:
            cache['is_node_drained'] = False
            return False
        result = client.get(f'/cluster/nodes/{_HOSTNAME}/drain')
        cache['is_node_drained'] = result[0] is not None and result[0].decode() == 'true'
        return cache['is_node_drained']
    except:
        cache['is_node_drained'] = False
        return False


def is_core_node():
    """True iff this host is a core (storage) node that is not disabled.

    Cheap static short-circuit: non-storage hostnames are never core,
    so we skip the etcd call entirely. Storage nodes read a single
    per-host key — no need to enumerate the whole cluster.
    """
    cache = _cache()
    if 'is_core_node' in cache:
        return cache['is_core_node']
    if not _IS_ETCD_NODE:
        cache['is_core_node'] = False
        return False
    try:
        client = _get_etcd_client()
        if client is None:
            # etcd unreachable: fail open — assume core (matches the
            # original get_core_nodes() behaviour which had no failure
            # path and never produced False here).
            cache['is_core_node'] = True
            return True
        result = client.get(f'/cluster/nodes/{_HOSTNAME}/disabled')
        cache['is_core_node'] = not (result[0] and result[0].decode() == 'true')
        return cache['is_core_node']
    except:
        cache['is_core_node'] = True
        return True


def runtime_probe_context():
    """Build the production probe seam for the lite health API."""
    return HealthProbeContext(
        is_etcd_node=_IS_ETCD_NODE,
        is_storage_node=_IS_STORAGE_NODE,
        etcd_client=_get_etcd_client,
        service_status=check_service_status,
        port_open=check_port_open,
        http_get=requests.get,
        storage_leader=is_storage_leader,
        dhcp_leader=is_dhcp_leader,
        node_drained=is_node_drained,
        core_node=is_core_node,
    )


# ---- check functions (ported from app.py) --------------------------------


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
                details['master'] = _HOSTNAME
                details['interface'] = interfaces[0].get('ifname')
    except json.JSONDecodeError as e:
        details['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        details['error'] = str(e)
    return details


def check_vip_status():
    """Check VIP status using keepalived and ip commands"""
    vip_status = {
        'gateway_vip': _vip_status_for_ip(GATEWAY_VIP),
        'storage_vip': _vip_status_for_ip(STORAGE_VIP),
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


def _add_health_service(health_status, name, result, unhealthy_statuses=('unhealthy', 'error'),
                        degraded_statuses=('degraded',)):
    """Record one service result and roll it into the overall status."""
    health_status['services'][name] = result
    status = result['status']
    if status in unhealthy_statuses:
        health_status['overall'] = 'unhealthy'
    elif status in degraded_statuses and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'


def _required_service_health(active, port, service, leader_reason):
    """Status for a service required on the current leader."""
    del service
    healthy = active and port
    return {
        'status': 'healthy' if healthy else 'unhealthy',
        'details': {
            'service_active': active,
            'port_open': port,
            'required': True,
            'reason': leader_reason,
        }
    }


def _standby_service_health(active, port, service_reason):
    """Status for a leader-elected service on a non-leader."""
    running = active or port
    reason = ('split-brain: running on non-leader' if running
              else service_reason)
    return {
        'status': 'unhealthy' if running else 'standby',
        'details': {
            'service_active': active,
            'port_open': port,
            'required': False,
            'reason': reason,
        }
    }


def _tang_advertisement_status(probe):
    """Probe the Tang advertisement endpoint.

    Returns (healthy, keys, error) — healthy is True for an HTTP 200,
    keys is the advertised key count (or 'unknown'), and error carries a
    human-readable failure description.
    """
    try:
        response = probe.http_get('http://localhost:8777/adv', timeout=5)
    except requests.exceptions.Timeout:
        return False, None, 'Tang advertisement timeout'
    except requests.exceptions.ConnectionError:
        return False, None, 'Tang connection failed'
    except Exception as e:
        return False, None, f'Tang advertisement failed: {str(e)}'

    if response.status_code != 200:
        return False, None, f'Tang advertisement returned HTTP {response.status_code}'
    try:
        data = response.json()
        if isinstance(data, dict) and 'keys' in data:
            return True, len(data['keys']), None
        return True, 'unknown', None
    except Exception:
        return True, 'unknown', None


def _tang_health_from_probe(running, port_open, healthy, keys, error):
    """Derive status/message from the service, port, and advertisement probes."""
    if running and port_open and healthy:
        return 'healthy', f'Tang server running and healthy ({keys} keys)'
    if running and port_open:
        return 'degraded', f'Tang service running but advertisement failed: {error}'
    if running:
        return 'unhealthy', f'Tang service running but port not accessible: {error}'
    return 'unhealthy', 'Tang service not running'


def check_tang_service(probe=None):
    """Check Tang server status and functionality."""
    probe = probe or runtime_probe_context()
    try:
        running = probe.service_status('tang-server.service')
        port_open = probe.port_open('localhost', 8777)

        healthy, keys, error = (False, None, None)
        if port_open:
            healthy, keys, error = _tang_advertisement_status(probe)

        status, message = _tang_health_from_probe(
            running, port_open, healthy, keys, error)
        return {
            'status': status,
            'details': {
                'service_active': running,
                'port_open': port_open,
                'advertisement_working': healthy,
                'key_count': keys,
                'message': message,
                'error': error
            }
        }
    except Exception as e:
        return {'status': 'error', 'details': {'message': f'Tang check failed: {str(e)}'}}


