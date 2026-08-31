"""Health API — lite service for non-storage nodes.

Serves /api/health + /metrics on :12724 on every managed node. Avoids
loading the heavy admin-api Flask app (vmm, authentik_manager, psycopg2,
scapy) on compute/nv/nas/x hosts.

All check functions are ported verbatim from app.py's get_comprehensive_health()
so the two surfaces report the same 21 service keys.
"""

import json
import os
import socket
import subprocess

import dns.resolver
import requests
from flask import Flask, g, jsonify

app = Flask(__name__)

GATEWAY_VIP = "10.0.0.254"
STORAGE_VIP = "10.0.0.100"
HEALTH_API_PORT = 12724

ETCD_PREFIX = '/cluster/nodes'

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

@app.before_request
def _init_request_cache():
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

def check_ceph_status():
    """Check Ceph cluster health"""
    try:
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
            return {'status': status, 'details': health_output}
        else:
            return {'status': 'error', 'details': result.stderr.strip()}
    except:
        return {'status': 'unavailable', 'details': 'ceph command failed'}


def check_dns_status():
    """Check DNS (dnsmasq) service and functionality"""
    try:
        service_running = check_service_status("dnsmasq")
        dns_working = False
        dns_details = "DNS query failed"
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ["127.0.0.1"]
            resolver.timeout = 3
            resolver.lifetime = 5
            local_hostname = _HOSTNAME
            answer = resolver.resolve(local_hostname, "A")
            if answer:
                resolved_ips = [str(rdata) for rdata in answer]
                dns_working = True
                dns_details = (
                    f"Local DNS server responding "
                    f"({local_hostname} -> {', '.join(resolved_ips)})"
                )
            else:
                dns_details = (
                    f"Local DNS query for {local_hostname} returned no results"
                )
        except dns.resolver.Timeout:
            dns_details = "Local DNS query timeout"
        except dns.resolver.NXDOMAIN:
            dns_details = "Local DNS query: domain not found"
        except dns.resolver.NoAnswer:
            dns_details = "Local DNS query: no answer"
        except Exception as e:
            dns_details = f"Local DNS query error: {str(e)}"

        if service_running and dns_working:
            status = "healthy"
        elif service_running:
            status = "degraded"
        else:
            status = "unhealthy"
        return {
            "status": status,
            "details": {
                "service_active": service_running,
                "dns_working": dns_working,
                "message": dns_details,
            },
        }
    except Exception as e:
        return {"status": "error", "details": f"DNS check failed: {str(e)}"}


def check_clock_skew():
    """Check clock skew using NTP protocol to VIP"""
    import ntplib

    ntp_server = '10.0.0.254'
    try:
        client = ntplib.NTPClient()
        response = client.request(ntp_server, version=3, timeout=2)
        offset_ms = response.offset * 1000

        # Thresholds: storage=100/1000, non-storage=1000/10000
        if _IS_STORAGE_NODE:
            warning_ms, critical_ms = 100, 1000
        else:
            warning_ms, critical_ms = 1000, 10000
        magnitude = abs(offset_ms)
        if magnitude > critical_ms:
            status = 'critical'
        elif magnitude > warning_ms:
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
        return {'status': 'error', 'details': {'message': f'NTP request failed: {str(e)}'}}
    except socket.gaierror:
        return {'status': 'error', 'details': {'message': f'Could not resolve NTP server {ntp_server}'}}
    except Exception as e:
        return {'status': 'error', 'details': {'message': f'Clock skew check failed: {str(e)}'}}


def check_certificate_expiry():
    """Check TLS certificate expiry from etcd"""
    if not _IS_ETCD_NODE:
        return {
            'status': 'not_applicable',
            'details': {
                'message': 'certificate check is core-only (s*)',
                'days_until_expiry': None,
                'expires_at': None
            }
        }
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        from datetime import datetime, UTC

        client = _get_etcd_client()
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
        time_until_expiry = expires_at - now
        days_until_expiry = time_until_expiry.days

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


def check_docker_daemon():
    """Check Docker daemon status"""
    try:
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
        return {'status': 'error', 'details': {'message': f'Docker check failed: {str(e)}'}}


def check_docker_registry():
    """Check Docker registry status and functionality"""
    try:
        registry_service_running = check_service_status('docker-registry')
        registry_container_running = False
        registry_port_open = check_port_open('localhost', 5000)

        registry_healthy = False
        registry_error = None
        registry_version = None

        if registry_port_open:
            test_urls = ['http://localhost:5000/v2/', 'http://10.0.0.100:5000/v2/']
            for url in test_urls:
                try:
                    health_response = requests.get(url, timeout=5)
                    if health_response.status_code == 200:
                        registry_healthy = True
                        registry_version = health_response.headers.get(
                            'Docker-Distribution-Api-Version', 'unknown')
                        break
                    else:
                        registry_error = f'Registry health check returned HTTP {health_response.status_code}'
                except requests.exceptions.Timeout:
                    registry_error = 'Registry health check timeout'
                except requests.exceptions.ConnectionError:
                    registry_error = 'Registry connection failed'
                except Exception as e:
                    registry_error = f'Registry health check failed: {str(e)}'

        registry_running = registry_service_running or registry_container_running
        is_storage_lead = is_storage_leader()

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
        return {'status': 'error', 'details': {'message': f'Registry check failed: {str(e)}'}}


def check_tang_service():
    """Check Tang server status and functionality"""
    try:
        tang_service_running = check_service_status('tang-server.service')
        tang_port_open = check_port_open('localhost', 8777)

        tang_healthy = False
        tang_error = None
        tang_keys = None

        if tang_port_open:
            try:
                adv_response = requests.get('http://localhost:8777/adv', timeout=5)
                if adv_response.status_code == 200:
                    tang_healthy = True
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
        return {'status': 'error', 'details': {'message': f'Tang check failed: {str(e)}'}}


def check_secrets_mount():
    """Check if /secrets is mounted"""
    try:
        result = subprocess.run(['mountpoint', '-q', '/secrets'],
                              capture_output=True, text=True, timeout=5)
        is_mounted = result.returncode == 0

        mount_details = None
        if is_mounted:
            try:
                mount_result = subprocess.run(
                    ['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS', '/secrets'],
                    capture_output=True, text=True, timeout=5)
                if mount_result.returncode == 0:
                    mount_details = mount_result.stdout.strip()
            except:
                pass

        secrets_accessible = False
        secrets_error = None
        try:
            if os.path.exists('/secrets') and os.path.isdir('/secrets'):
                os.listdir('/secrets')
                secrets_accessible = True
            else:
                secrets_error = '/secrets directory does not exist'
        except PermissionError:
            secrets_accessible = is_mounted
            secrets_error = None
        except Exception as e:
            secrets_error = f'Error accessing /secrets: {str(e)}'

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
        return {'status': 'error', 'details': {'message': f'Secrets mount check failed: {str(e)}'}}


def check_open_webui():
    """Check Open-WebUI service status and functionality"""
    try:
        webui_service_running = check_service_status('open-webui')
        webui_port_open = check_port_open('localhost', 8380)

        webui_healthy = False
        webui_error = None
        webui_version = None

        if webui_port_open:
            try:
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

        is_storage_lead = is_storage_leader()

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
        return {'status': 'error', 'details': {'message': f'Open-WebUI check failed: {str(e)}'}}


def check_vip_status():
    """Check VIP status using keepalived and ip commands"""
    gateway_vip_ip = GATEWAY_VIP
    storage_vip_ip = STORAGE_VIP
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
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', gateway_vip_ip],
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            interfaces = json.loads(result.stdout)
            if interfaces:
                vip_status['gateway_vip']['active'] = True
                vip_status['gateway_vip']['master'] = _HOSTNAME
                vip_status['gateway_vip']['interface'] = interfaces[0].get('ifname')
        else:
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
                vip_status['storage_vip']['master'] = _HOSTNAME
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


# ---- comprehensive health (matches app.py's get_comprehensive_health) ----

def _add_health_service(health_status, name, result, unhealthy_statuses=('unhealthy', 'error'),
                        degraded_statuses=('degraded',)):
    """Record one service result and roll it into the overall status."""
    health_status['services'][name] = result
    status = result['status']
    if status in unhealthy_statuses:
        health_status['overall'] = 'unhealthy'
    elif status in degraded_statuses and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'


def _etcd_health():
    if not _IS_ETCD_NODE:
        return {'status': 'not_applicable',
                'details': {'reason': 'admin-api uses etcd only on storage nodes'}}
    try:
        client = _get_etcd_client()
        client.get('/test')
        return {'status': 'healthy', 'details': 'connected'}
    except Exception as e:
        return {'status': 'unhealthy', 'details': str(e)}


def _ceph_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_ceph_status()


def _required_service_health(active, port):
    healthy = active and port
    return {
        'status': 'healthy' if healthy else 'unhealthy',
        'details': {
            'service_active': active, 'port_open': port,
            'required': True, 'reason': 'storage leader'
        }
    }


def _standby_service_health(active, port, service_reason):
    running = active or port
    if running:
        reason = 'split-brain: running on non-leader'
        status = 'unhealthy'
    else:
        reason = service_reason
        status = 'standby'
    return {
        'status': status,
        'details': {
            'service_active': active, 'port_open': port,
            'required': False, 'reason': reason
        }
    }


def _postgres_health(is_storage_lead):
    running = check_service_status('postgresql@16-main')
    port = check_port_open('localhost', 5432)
    if is_storage_lead:
        return _required_service_health(running, port)
    return _standby_service_health(running, port, 'not storage leader')


def _qdrant_health(is_storage_lead):
    running = check_service_status('qdrant')
    port = check_port_open('localhost', 6333)
    if is_storage_lead:
        return _required_service_health(running, port)
    return _standby_service_health(running, port, 'not storage leader')


def _leader_election_health(is_storage_node, systemd_name):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    running = check_service_status(systemd_name)
    return {'status': 'healthy' if running else 'unhealthy',
            'details': {'service_active': running}}


def _dhcp_health(is_dhcp_lead):
    port = check_port_open('localhost', 8067)
    if is_dhcp_lead:
        return {'status': 'healthy' if port else 'unhealthy',
                'details': {'health_port_open': port, 'required': True,
                            'reason': 'dhcp leader'}}
    running = port
    reason = ('split-brain: dhcp running on non-leader' if running
              else 'not dhcp leader')
    return {'status': 'unhealthy' if running else 'standby',
            'details': {'health_port_open': port, 'required': False,
                        'reason': reason}}


def _squid_health():
    running = check_service_status('squid')
    port = check_port_open('localhost', 3128)
    functional = False
    error = None
    if running and port:
        try:
            response = requests.get(
                'http://localhost:12723/api/ping',
                proxies={'http': 'http://localhost:3128'}, timeout=5)
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
    return {'status': 'healthy' if healthy else 'unhealthy',
            'details': {'service_active': running, 'port_open': port,
                        'proxy_functional': functional, 'error': error}}


def _ntp_health():
    running = check_service_status('ntp') or check_service_status('chrony')
    return {'status': 'healthy' if running else 'unhealthy',
            'details': {'service_active': running}}


def _keepalived_health():
    if is_core_node():
        running = check_service_status('keepalived')
        return {'status': 'healthy' if running else 'unhealthy',
                'details': {'service_active': running}}
    running = check_service_status('keepalived')
    if running:
        return {'status': 'unhealthy',
                'details': {'service_active': running,
                            'reason': 'split-brain: keepalived running on non-core node'}}
    return {'status': 'standby',
            'details': {'service_active': running, 'reason': 'not a core node'}}


def _storage_only_health(name, check_fn, is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_fn()


def get_comprehensive_health():
    """Get comprehensive health data — matches admin-api's output exactly."""
    health_status = {'overall': 'healthy', 'services': {}}
    is_storage_node = _IS_STORAGE_NODE

    _add_health_service(health_status, 'etcd', _etcd_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'ceph', _ceph_health(is_storage_node),
                        ('unhealthy', 'error', 'standby', 'unknown'), ())
    is_storage_lead = is_storage_leader()
    _add_health_service(health_status, 'postgresql', _postgres_health(is_storage_lead), ('unhealthy',), ())
    _add_health_service(health_status, 'qdrant', _qdrant_health(is_storage_lead), ('unhealthy',), ())
    _add_health_service(health_status, 'storage_leader_election', _leader_election_health(is_storage_node, 'storage-leader-election'), ('unhealthy',), ())
    _add_health_service(health_status, 'dhcp_leader_election', _leader_election_health(is_storage_node, 'dhcp-leader-election'), ('unhealthy',), ())
    _add_health_service(health_status, 'dhcp', _dhcp_health(is_dhcp_leader()), ('unhealthy',), ())
    _add_health_service(health_status, 'dns', check_dns_status(), ('unhealthy',), ())
    _add_health_service(health_status, 'squid', _squid_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'ntp', _ntp_health(), ('unhealthy',), ())
    _add_health_service(health_status, 'tls_certificate', check_certificate_expiry(), ('expired', 'critical'), ('warning',))
    _add_health_service(health_status, 'clock_skew', check_clock_skew(), ('critical', 'error'), ('warning',))
    _add_health_service(health_status, 'docker_daemon', _storage_only_health('docker_daemon', check_docker_daemon, is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'docker_registry', check_docker_registry(), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'tang', _storage_only_health('tang', check_tang_service, is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'secrets_mount', _storage_only_health('secrets_mount', check_secrets_mount, is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'open_webui', check_open_webui(), ('unhealthy', 'error'), ('degraded',))

    vip_health = check_vip_status()
    for name, key in [('gateway_vip', 'gateway_vip'), ('storage_vip', 'storage_vip')]:
        active = vip_health[key]['active']
        result = {'status': 'healthy' if active else 'standby', 'details': vip_health[key]}
        _add_health_service(health_status, name, result, (), ())
    _add_health_service(health_status, 'keepalived', _keepalived_health(), ('unhealthy',), ())

    health_status['storage_leader'] = is_storage_lead
    health_status['dhcp_leader'] = is_dhcp_leader()
    health_status['drained'] = is_node_drained()
    return health_status


# ---- routes -------------------------------------------------------------

@app.route('/api/health')
def health():
    health_status = get_comprehensive_health()
    status_code = 200 if health_status['overall'] == 'healthy' else 503
    return jsonify(health_status), status_code


@app.route('/metrics')
def prometheus_metrics():
    try:
        health_data = get_comprehensive_health()
        metrics = []

        overall_value = 1 if health_data['overall'] == 'healthy' else 0
        metrics.append(f'ycluster_node_healthy{{node="{_HOSTNAME}"}} {overall_value}')

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
            metrics.append(
                f'ycluster_service_health{{node="{_HOSTNAME}",'
                f'service="{service}"}} {service_value}'
            )

        # vip metrics
        vip_status = check_vip_status()
        gateway_vip_active = 1 if vip_status['gateway_vip']['active'] else 0
        storage_vip_active = 1 if vip_status['storage_vip']['active'] else 0
        metrics.append(
            f'ycluster_vip_active{{node="{_HOSTNAME}",'
            f'vip="gateway"}} {gateway_vip_active}'
        )
        metrics.append(
            f'ycluster_vip_active{{node="{_HOSTNAME}",'
            f'vip="storage"}} {storage_vip_active}'
        )

        response = "\n".join(metrics) + "\n"
        return response, 200, {
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
        }
    except Exception as e:
        error_response = (
            f'ycluster_metrics_error{{node="{_HOSTNAME}"}} 1\n'
        )
        return error_response, 500, {
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
        }


if __name__ == "__main__":
    # Bind on all interfaces so admin-api on storage peers can fetch
    # each node's health cross-node via http://<ip>:12724/api/health.
    # The endpoint exposes only read-only health data + Prometheus
    # metrics (no auth, no mutations); firewall scopes who can reach
    # the cluster network. waitress (python3-waitress, installed by
    # setup-web-services.yml) is the production WSGI server — matches
    # what admin-api (app.py) already uses.
    from waitress import serve
    serve(app, host="0.0.0.0", port=HEALTH_API_PORT, threads=8)
