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
    _IS_ETCD_NODE,
    _IS_STORAGE_NODE,
    _get_etcd_client,
    check_port_open,
    check_service_status,
    check_vip_status,
    is_core_node,
    is_dhcp_leader,
    is_node_drained,
    is_storage_leader,
)

# Comprehensive health (matches app.py's get_comprehensive_health) ----

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


def service_metric_value(status):
    """Map a service status to the metrics API's ordinal."""
    return {'healthy': 0, 'degraded': 1, 'standby': 3}.get(status, 2)


