"""Service-specific health checks for the lite health API."""

import json
import os
import socket
import subprocess

import dns.resolver
import requests

from health_common import (
    ETCD_PREFIX,
    STORAGE_VIP,
    _get_etcd_client,
    _HOSTNAME,
    _IS_ETCD_NODE,
    _IS_STORAGE_NODE,
    _tang_advertisement_status,
    _tang_health_from_probe,
    check_port_open,
    check_service_status,
    check_tang_service,
    is_core_node,
    is_dhcp_leader,
    is_node_drained,
    is_storage_leader,
)

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


def _registry_probe_status():
    """Probe the Docker registry /v2/ endpoint; localhost, then storage VIP."""
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
        registry_service_running = check_service_status('docker-registry')
        registry_container_running = False
        registry_port_open = check_port_open('localhost', 5000)
        registry_healthy, registry_version, registry_error = (False, None, None)

        if registry_port_open:
            registry_healthy, registry_version, registry_error = _registry_probe_status()

        registry_running = registry_service_running or registry_container_running
        is_storage_lead = is_storage_leader()

        if is_storage_lead:
            status, message = _registry_leader_status(
                registry_running, registry_port_open, registry_healthy,
                registry_version, registry_error)
        elif registry_running or registry_port_open:
            status, message = 'unhealthy', 'Split-brain: Registry running on non-leader'
        else:
            status, message = 'standby', 'Registry not required (not storage leader)'

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


def _secrets_mount_details():
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
        except Exception:
            pass
    return is_mounted, mount_details


def _secrets_access(is_mounted):
    secrets_accessible = False
    secrets_error = None
    try:
        if os.path.exists('/secrets') and os.path.isdir('/secrets'):
            os.listdir('/secrets')
            secrets_accessible = True
        else:
            secrets_error = '/secrets directory does not exist'
    except PermissionError:
        # The service runs unprivileged and /secrets contents are deliberately
        # root-only; mounted-but-unreadable is the expected state.
        secrets_accessible = is_mounted
        secrets_error = None
    except Exception as e:
        secrets_error = f'Error accessing /secrets: {str(e)}'
    return secrets_accessible, secrets_error


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
        return {'status': 'error', 'details': {'message': f'Secrets mount check failed: {str(e)}'}}


def _open_webui_health_probe():
    """Probe the Open-WebUI health endpoint; returns (healthy, version, error)."""
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
        webui_service_running = check_service_status('open-webui')
        webui_port_open = check_port_open('localhost', 8380)
        webui_healthy, webui_version, webui_error = (False, None, None)

        if webui_port_open:
            webui_healthy, webui_version, webui_error = _open_webui_health_probe()

        is_storage_lead = is_storage_leader()

        if is_storage_lead:
            status, message = _open_webui_leader_status(
                webui_service_running, webui_port_open, webui_healthy,
                webui_version, webui_error)
        elif webui_service_running or webui_port_open:
            status, message = 'unhealthy', 'Split-brain: Open-WebUI running on non-leader'
        else:
            status, message = 'standby', 'Open-WebUI not required (not storage leader)'

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
