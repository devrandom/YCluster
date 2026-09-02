"""Aggregate health report for the lite health API."""

import requests

from health_checks import (
    check_ceph_status,
    check_certificate_expiry,
    check_clock_skew,
    check_docker_daemon,
    check_docker_registry,
    check_dns_status,
    check_open_webui,
    check_secrets_mount,
    check_tang_service,
)
from health_common import (
    _add_health_service,
    _required_service_health,
    _standby_service_health,
    check_vip_status,
    runtime_probe_context,
)

# Comprehensive health (matches app.py's get_comprehensive_health) ----

def _etcd_health(probe):
    if not probe.is_etcd_node:
        return {'status': 'not_applicable',
                'details': {'reason': 'admin-api uses etcd only on storage nodes'}}
    try:
        client = probe.etcd_client()
        client.get('/test')
        return {'status': 'healthy', 'details': 'connected'}
    except Exception as e:
        return {'status': 'unhealthy', 'details': str(e)}


def _ceph_health(is_storage_node):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    return check_ceph_status()


def _postgres_health(is_storage_lead, probe):
    running = probe.service_status('postgresql@16-main')
    port = probe.port_open('localhost', 5432)
    if is_storage_lead:
        return _required_service_health(running, port, 'postgresql', 'storage leader')
    return _standby_service_health(running, port, 'not storage leader')


def _qdrant_health(is_storage_lead, probe):
    running = probe.service_status('qdrant')
    port = probe.port_open('localhost', 6333)
    if is_storage_lead:
        return _required_service_health(running, port, 'qdrant', 'storage leader')
    return _standby_service_health(running, port, 'not storage leader')


def _leader_election_health(is_storage_node, systemd_name, probe):
    if not is_storage_node:
        return {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}}
    running = probe.service_status(systemd_name)
    return {'status': 'healthy' if running else 'unhealthy',
            'details': {'service_active': running}}


def _dhcp_health(is_dhcp_lead, probe):
    port = probe.port_open('localhost', 8067)
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


def _squid_health(probe):
    running = probe.service_status('squid')
    port = probe.port_open('localhost', 3128)
    functional = False
    error = None
    if running and port:
        try:
            response = probe.http_get(
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


def _ntp_health(probe):
    running = probe.service_status('ntp') or probe.service_status('chrony')
    return {'status': 'healthy' if running else 'unhealthy',
            'details': {'service_active': running}}


def _keepalived_health(probe):
    if probe.core_node():
        running = probe.service_status('keepalived')
        return {'status': 'healthy' if running else 'unhealthy',
                'details': {'service_active': running}}
    running = probe.service_status('keepalived')
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


def get_comprehensive_health(probe=None):
    """Get comprehensive health data for the lite health API."""
    probe = probe or runtime_probe_context()
    health_status = {'overall': 'healthy', 'services': {}}
    is_storage_node = probe.is_storage_node

    _add_health_service(health_status, 'etcd', _etcd_health(probe), ('unhealthy',), ())
    _add_health_service(health_status, 'ceph', _ceph_health(is_storage_node),
                        ('unhealthy', 'error', 'standby', 'unknown'), ())
    is_storage_lead = probe.storage_leader()
    _add_health_service(health_status, 'postgresql', _postgres_health(is_storage_lead, probe), ('unhealthy',), ())
    _add_health_service(health_status, 'qdrant', _qdrant_health(is_storage_lead, probe), ('unhealthy',), ())
    _add_health_service(health_status, 'storage_leader_election', _leader_election_health(is_storage_node, 'storage-leader-election', probe), ('unhealthy',), ())
    _add_health_service(health_status, 'dhcp_leader_election', _leader_election_health(is_storage_node, 'dhcp-leader-election', probe), ('unhealthy',), ())
    _add_health_service(health_status, 'dhcp', _dhcp_health(probe.dhcp_leader(), probe), ('unhealthy',), ())
    _add_health_service(health_status, 'dns', check_dns_status(), ('unhealthy',), ())
    _add_health_service(health_status, 'squid', _squid_health(probe), ('unhealthy',), ())
    _add_health_service(health_status, 'ntp', _ntp_health(probe), ('unhealthy',), ())
    _add_health_service(health_status, 'tls_certificate', check_certificate_expiry(), ('expired', 'critical'), ('warning',))
    _add_health_service(health_status, 'clock_skew', check_clock_skew(), ('critical', 'error'), ('warning',))
    _add_health_service(health_status, 'docker_daemon', _storage_only_health('docker_daemon', check_docker_daemon, is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'docker_registry', check_docker_registry(), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'tang', _storage_only_health('tang', lambda: check_tang_service(probe), is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'secrets_mount', _storage_only_health('secrets_mount', check_secrets_mount, is_storage_node), ('unhealthy', 'error'), ('degraded',))
    _add_health_service(health_status, 'open_webui', check_open_webui(), ('unhealthy', 'error'), ('degraded',))

    vip_health = check_vip_status()
    for name, key in [('gateway_vip', 'gateway_vip'), ('storage_vip', 'storage_vip')]:
        active = vip_health[key]['active']
        result = {'status': 'healthy' if active else 'standby', 'details': vip_health[key]}
        _add_health_service(health_status, name, result, (), ())
    _add_health_service(health_status, 'keepalived', _keepalived_health(probe), ('unhealthy',), ())

    health_status['storage_leader'] = is_storage_lead
    health_status['dhcp_leader'] = probe.dhcp_leader()
    health_status['drained'] = probe.node_drained()
    return health_status


def service_metric_value(status):
    """Map a service status to the metrics API's ordinal."""
    return {'healthy': 0, 'degraded': 1, 'standby': 3}.get(status, 2)


