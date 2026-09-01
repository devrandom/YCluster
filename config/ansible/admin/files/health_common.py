"""Shared state and node-role helpers for the lite health API."""

import json
import socket
import subprocess

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


