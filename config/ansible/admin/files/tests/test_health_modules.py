"""Focused tests for the split lite health API modules."""

import os
import sys

import json
import socket
import subprocess
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for _path in (_HERE, _FILES, os.path.join(_FILES, 'ycluster')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import health_common as hc
import health_report as hr


class HealthCommonTests(unittest.TestCase):
    def test_check_service_status(self):
        cases = [
            (subprocess.CompletedProcess([], 0, stdout='active\n', stderr=''), True),
            (subprocess.CompletedProcess([], 0, stdout='inactive\n', stderr=''), False),
        ]
        for result, expected in cases:
            with mock.patch.object(hc.subprocess, 'run', return_value=result):
                self.assertEqual(hc.check_service_status('svc'), expected)
        with mock.patch.object(hc.subprocess, 'run',
                               side_effect=RuntimeError('down')):
            self.assertFalse(hc.check_service_status('svc'))

    def test_check_port_open(self):
        with mock.patch.object(hc.socket, 'socket') as sock_cls:
            sock = sock_cls.return_value
            sock.connect_ex.return_value = 0
            self.assertTrue(hc.check_port_open('host', 80))
            sock.close.assert_called_once()
        with mock.patch.object(hc.socket, 'socket') as sock_cls:
            sock_cls.return_value.connect_ex.return_value = 1
            self.assertFalse(hc.check_port_open('host', 80))
        with mock.patch.object(hc.socket, 'socket',
                               side_effect=OSError('bad')):
            self.assertFalse(hc.check_port_open('host', 80))

    def test_get_etcd_client_cache_and_failure(self):
        from ycluster.common import etcd_utils
        with ha_request_context():
            hc.init_request_cache()
            client = mock.Mock()
            with mock.patch.object(hc, '_IS_ETCD_NODE', True), \
                    mock.patch.object(etcd_utils, 'get_etcd_client',
                                      return_value=client):
                self.assertIs(hc._get_etcd_client(), client)
                self.assertIs(hc._get_etcd_client(), client)
        with ha_request_context():
            hc.init_request_cache()
            with mock.patch.object(etcd_utils, 'get_etcd_client',
                                   side_effect=RuntimeError('down')):
                self.assertIsNone(hc._get_etcd_client())

    def test_leader_and_node_state_caching(self):
        client = mock.Mock()
        client.get.side_effect = [
            (b'node-a',), (b'node-b',), (b'true',), (None,), (None,)]
        with ha_request_context(), mock.patch.object(
                hc, '_IS_ETCD_NODE', True), \
                mock.patch.object(hc, '_HOSTNAME', 'node-a'), \
                mock.patch.object(hc, '_get_etcd_client',
                                  return_value=client):
            hc.init_request_cache()
            self.assertTrue(hc.is_storage_leader())
            self.assertFalse(hc.is_dhcp_leader())
            self.assertTrue(hc.is_node_drained())
            self.assertTrue(hc.is_core_node())
            self.assertTrue(hc.is_core_node())
        client.get.assert_any_call('/cluster/leader/app')
        client.get.assert_any_call('/cluster/leader/dhcp')

    def test_node_state_non_storage_and_cache(self):
        with ha_request_context(), mock.patch.object(
                hc, '_IS_ETCD_NODE', False):
            hc.init_request_cache()
            hc.g._cache.update({
                'is_storage_leader': True,
                'is_dhcp_leader': True,
                'is_node_drained': True,
                'is_core_node': True,
            })
            self.assertTrue(hc.is_storage_leader())
            self.assertTrue(hc.is_dhcp_leader())
            self.assertTrue(hc.is_node_drained())
            self.assertTrue(hc.is_core_node())

    def test_node_state_non_storage_uncached(self):
        with ha_request_context(), mock.patch.object(
                hc, '_IS_ETCD_NODE', False):
            hc.init_request_cache()
            self.assertFalse(hc.is_storage_leader())
            self.assertFalse(hc.is_dhcp_leader())
            self.assertFalse(hc.is_node_drained())
            self.assertFalse(hc.is_core_node())

    def test_node_state_client_none_and_exception(self):
        with ha_request_context(), mock.patch.object(
                hc, '_IS_ETCD_NODE', True), \
                mock.patch.object(hc, '_get_etcd_client', return_value=None):
            hc.init_request_cache()
            self.assertFalse(hc.is_storage_leader())
            self.assertFalse(hc.is_dhcp_leader())
            self.assertFalse(hc.is_node_drained())
            self.assertTrue(hc.is_core_node())
        with ha_request_context(), mock.patch.object(
                hc, '_IS_ETCD_NODE', True), \
                mock.patch.object(hc, '_get_etcd_client',
                                  side_effect=RuntimeError('down')):
            hc.init_request_cache()
            self.assertFalse(hc.is_storage_leader())
            self.assertFalse(hc.is_dhcp_leader())
            self.assertFalse(hc.is_node_drained())
            self.assertTrue(hc.is_core_node())

    def test_vip_status(self):
        results = [
            subprocess.CompletedProcess([], 0, stdout='[{"ifname":"eth0"}]',
                                        stderr=''),
            subprocess.CompletedProcess([], 1, stdout='', stderr=''),
        ]
        with mock.patch.object(hc.subprocess, 'run', side_effect=results), \
                mock.patch.object(hc, 'check_service_status',
                                  return_value=True):
            status = hc.check_vip_status()
        self.assertTrue(status['gateway_vip']['active'])
        self.assertFalse(status['storage_vip']['active'])
        self.assertTrue(status['keepalived_service']['active'])


class HealthReportHelperTests(unittest.TestCase):
    def test_add_health_service(self):
        health = {'overall': 'healthy', 'services': {}}
        hr._add_health_service(health, 'a', {'status': 'unhealthy'})
        self.assertEqual(health['overall'], 'unhealthy')
        hr._add_health_service(health, 'b', {'status': 'degraded'})
        hr._add_health_service(health, 'd', {'status': 'degraded'},
                               (), ('degraded',))
        hr._add_health_service(health, 'c', {'status': 'healthy'})
        self.assertEqual(health['services']['c']['status'], 'healthy')

    def test_etcd_health(self):
        self.assertEqual(hr._etcd_health(), {'status': 'not_applicable',
                                             'details': {'reason': 'admin-api uses etcd only on storage nodes'}})
        with mock.patch.object(hr, '_IS_ETCD_NODE', True), \
                mock.patch.object(hr, '_get_etcd_client',
                                  return_value=mock.Mock(get=lambda _: None)):
            self.assertEqual(hr._etcd_health(),
                             {'status': 'healthy', 'details': 'connected'})
        with mock.patch.object(hr, '_IS_ETCD_NODE', True), \
                mock.patch.object(hr, '_get_etcd_client',
                                  side_effect=RuntimeError('down')):
            self.assertEqual(hr._etcd_health(),
                             {'status': 'unhealthy', 'details': 'down'})

    def test_ceph_health(self):
        self.assertEqual(hr._ceph_health(False),
                         {'status': 'not_applicable',
                          'details': {'reason': 'not required on compute nodes'}})
        with mock.patch.object(hr, 'check_ceph_status',
                               return_value={'status': 'healthy'}):
            self.assertEqual(hr._ceph_health(True), {'status': 'healthy'})

    def test_service_helpers(self):
        self.assertEqual(hr._required_service_health(True, True)['status'],
                         'healthy')
        self.assertEqual(hr._required_service_health(True, False)['status'],
                         'unhealthy')
        self.assertEqual(hr._standby_service_health(False, False, 'why'),
                         {'status': 'standby',
                          'details': {'service_active': False, 'port_open': False,
                                      'required': False, 'reason': 'why'}})
        with mock.patch.object(hr, 'check_service_status', return_value=True), \
                mock.patch.object(hr, 'check_port_open', return_value=True):
            self.assertEqual(hr._postgres_health(True)['status'], 'healthy')
            self.assertEqual(hr._qdrant_health(False)['status'], 'unhealthy')
        self.assertEqual(
            hr._standby_service_health(True, False, 'why')['status'],
            'unhealthy')
        self.assertEqual(hr._leader_election_health(False, 'svc'),
                         {'status': 'not_applicable',
                          'details': {'reason': 'not required on compute nodes'}})
        with mock.patch.object(hr, 'check_service_status', return_value=True):
            self.assertEqual(hr._leader_election_health(True, 'svc')['status'],
                             'healthy')
        self.assertEqual(hr._dhcp_health(True),
                         {'status': 'unhealthy',
                          'details': {'health_port_open': False, 'required': True,
                                      'reason': 'dhcp leader'}})
        self.assertEqual(hr._dhcp_health(False),
                         {'status': 'standby',
                          'details': {'health_port_open': False, 'required': False,
                                      'reason': 'not dhcp leader'}})
        with mock.patch.object(hr, 'check_service_status',
                               side_effect=[True, False]):
            self.assertEqual(hr._ntp_health()['status'], 'healthy')
        with mock.patch.object(hr, 'check_service_status', return_value=True), \
                mock.patch.object(hr, 'check_port_open', return_value=True), \
                mock.patch.object(hr.requests, 'get',
                                  side_effect=hr.requests.exceptions.ProxyError):
            self.assertEqual(hr._squid_health()['status'], 'unhealthy')
        self.assertEqual(hr._storage_only_health('x', lambda: 'ok', False),
                         {'status': 'not_applicable',
                          'details': {'reason': 'not required on compute nodes'}})

    def test_keepalived_health(self):
        with mock.patch.object(hr, 'is_core_node', return_value=True), \
                mock.patch.object(hr, 'check_service_status',
                                  return_value=True):
            self.assertEqual(hr._keepalived_health()['status'], 'healthy')
        with mock.patch.object(hr, 'is_core_node', return_value=False), \
                mock.patch.object(hr, 'check_service_status',
                                  return_value=True):
            self.assertEqual(hr._keepalived_health()['status'], 'unhealthy')
        with mock.patch.object(hr, 'is_core_node', return_value=False), \
                mock.patch.object(hr, 'check_service_status',
                                  return_value=False):
            self.assertEqual(hr._keepalived_health()['status'], 'standby')

    def test_squid_health(self):
        with mock.patch.object(hr, 'check_service_status', return_value=True), \
                mock.patch.object(hr, 'check_port_open', return_value=True), \
                mock.patch.object(hr.requests, 'get', return_value=mock.Mock(
                    status_code=503)):
            self.assertEqual(hr._squid_health()['status'], 'healthy')
        with mock.patch.object(hr, 'check_service_status', return_value=True), \
                mock.patch.object(hr, 'check_port_open', return_value=True), \
                mock.patch.object(hr.requests, 'get',
                                  side_effect=hr.requests.exceptions.Timeout):
            self.assertEqual(hr._squid_health()['status'], 'unhealthy')


class ComprehensiveHealthTests(unittest.TestCase):
    def test_storage_only_and_squid(self):
        self.assertEqual(hr._storage_only_health('x', lambda: 'ok', False),
                         {'status': 'not_applicable',
                          'details': {'reason': 'not required on compute nodes'}})
        self.assertEqual(hr._storage_only_health('x', lambda: 'ok', True), 'ok')
        with mock.patch.object(hr, 'check_service_status', return_value=True), \
                mock.patch.object(hr, 'check_port_open', return_value=True), \
                mock.patch.object(hr.requests, 'get', side_effect=OSError('bad')):
            self.assertEqual(hr._squid_health()['status'], 'unhealthy')

    def test_comprehensive_health(self):
        result = {'status': 'healthy'}
        patches = {
            '_etcd_health': mock.Mock(return_value=result),
            '_ceph_health': mock.Mock(return_value=result),
            '_postgres_health': mock.Mock(return_value=result),
            '_qdrant_health': mock.Mock(return_value=result),
            '_leader_election_health': mock.Mock(return_value=result),
            '_dhcp_health': mock.Mock(return_value=result),
            '_squid_health': mock.Mock(return_value=result),
            '_ntp_health': mock.Mock(return_value=result),
            '_storage_only_health': mock.Mock(return_value=result),
            '_keepalived_health': mock.Mock(return_value=result),
            'check_dns_status': mock.Mock(return_value=result),
            'check_certificate_expiry': mock.Mock(return_value=result),
            'check_clock_skew': mock.Mock(return_value=result),
            'check_docker_registry': mock.Mock(return_value=result),
            'check_open_webui': mock.Mock(return_value=result),
            'check_vip_status': mock.Mock(return_value={
                'gateway_vip': {'active': True},
                'storage_vip': {'active': False}}),
            'is_storage_leader': mock.Mock(return_value=True),
            'is_dhcp_leader': mock.Mock(return_value=False),
            'is_node_drained': mock.Mock(return_value=False),
        }
        with mock.patch.multiple(hr, **patches):
            health = hr.get_comprehensive_health()
        self.assertEqual(health['overall'], 'healthy')
        self.assertTrue(health['storage_leader'])
        self.assertFalse(health['dhcp_leader'])
        self.assertIn('gateway_vip', health['services'])


class HealthApiRoutesTests(unittest.TestCase):
    def setUp(self):
        self.ha = get_health_api()
        self.client = self.ha.app.test_client()

    def test_health_route(self):
        with mock.patch.object(self.ha, 'get_comprehensive_health',
                               return_value={'overall': 'healthy',
                                             'services': {}}):
            response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json())

    def test_metrics_route(self):
        health = {'overall': 'healthy', 'services': {'etcd': {'status': 'healthy'}}}
        with mock.patch.object(self.ha, 'get_comprehensive_health',
                               return_value=health), \
                mock.patch.object(self.ha, 'check_vip_status',
                                  return_value={
                                      'gateway_vip': {'active': True},
                                      'storage_vip': {'active': False}}):
            response = self.client.get('/metrics')
        self.assertEqual(response.status_code, 200)
        self.assertIn('ycluster_node_healthy', response.get_data(as_text=True))

    def test_metrics_route_error(self):
        with mock.patch.object(self.ha, 'get_comprehensive_health',
                               side_effect=RuntimeError('down')):
            response = self.client.get('/metrics')
        self.assertEqual(response.status_code, 500)


class HealthChecksExtraTests(unittest.TestCase):
    def setUp(self):
        import health_checks as hs
        self.hs = hs

    def test_ceph_status_branches(self):
        results = [
            subprocess.CompletedProcess([], 0, stdout='HEALTH_OK\n', stderr=''),
            subprocess.CompletedProcess([], 0, stdout='HEALTH_WARN\n', stderr=''),
            subprocess.CompletedProcess([], 1, stdout='', stderr='bad'),
        ]
        for result, expected in zip(results, ['healthy', 'degraded', 'error']):
            with mock.patch.object(self.hs.subprocess, 'run',
                                   return_value=result):
                self.assertEqual(self.hs.check_ceph_status()['status'], expected)
        result = subprocess.CompletedProcess([], 0, stdout='HEALTH_ERR\n',
                                             stderr='')
        with mock.patch.object(self.hs.subprocess, 'run', return_value=result):
            self.assertEqual(self.hs.check_ceph_status()['status'], 'unhealthy')
        with mock.patch.object(self.hs.subprocess, 'run',
                               side_effect=OSError('missing')):
            self.assertEqual(self.hs.check_ceph_status()['status'],
                             'unavailable')

    def test_dns_status_branches(self):
        resolver = mock.Mock()
        resolver.resolve.return_value = ['1.1.1.1']
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            result = self.hs.check_dns_status()
        self.assertEqual(result['status'], 'healthy')
        resolver.resolve.side_effect = self.hs.dns.resolver.NXDOMAIN
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            self.assertEqual(self.hs.check_dns_status()['status'], 'degraded')
        with mock.patch.object(self.hs, 'check_service_status',
                               side_effect=RuntimeError('bad')):
            self.assertEqual(self.hs.check_dns_status()['status'], 'error')

    def test_certificate_expiry(self):
        from cryptography import x509
        from datetime import datetime, timedelta
        cert = mock.Mock()
        cert.not_valid_after = datetime.now() + timedelta(days=20)
        cert.subject.rfc4514_string.return_value = 'CN=node'
        cert.issuer.rfc4514_string.return_value = 'CN=ca'
        with mock.patch.object(self.hs, '_IS_ETCD_NODE', False):
            self.assertEqual(
                self.hs.check_certificate_expiry()['status'],
                'not_applicable')
        client = mock.Mock(get=lambda _: (None, None))
        with mock.patch.object(self.hs, '_IS_ETCD_NODE', True), \
                mock.patch.object(self.hs, '_get_etcd_client',
                                  return_value=client):
            self.assertEqual(
                self.hs.check_certificate_expiry()['status'],
                'not_configured')
        client = mock.Mock(get=lambda _: (b'not-a-cert', None))
        with mock.patch.object(self.hs, '_IS_ETCD_NODE', True), \
                mock.patch.object(self.hs, '_get_etcd_client',
                                  return_value=client), \
                mock.patch.object(x509, 'load_pem_x509_certificate',
                                  return_value=cert):
            result = self.hs.check_certificate_expiry()
        self.assertEqual(result['status'], 'warning')

    def test_registry_checks(self):
        response = mock.Mock(status_code=200,
                             headers={'Docker-Distribution-Api-Version': 'v2'})
        with mock.patch.object(self.hs.requests, 'get', return_value=response):
            self.assertEqual(self.hs._registry_probe_status(),
                             (True, 'v2', None))
        response = mock.Mock(status_code=500)
        with mock.patch.object(self.hs.requests, 'get', side_effect=[
                response, response]):
            _, _, error = self.hs._registry_probe_status()
        self.assertEqual(error, 'Registry health check returned HTTP 500')
        with mock.patch.object(self.hs.requests, 'get',
                               side_effect=self.hs.requests.exceptions
                               .ConnectionError('down')):
            _, _, error = self.hs._registry_probe_status()
        self.assertEqual(error, 'Registry connection failed')
        self.assertEqual(
            self.hs._registry_leader_status(False, False, False, None, None),
            ('unhealthy', 'Registry not running'))
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=False), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=False):
            self.assertEqual(self.hs.check_docker_registry()['status'],
                             'standby')
        with mock.patch.object(self.hs, 'check_service_status',
                               side_effect=RuntimeError('bad')):
            self.assertEqual(self.hs.check_docker_registry()['status'], 'error')

    def test_secrets_mount(self):
        with mock.patch.object(self.hs.subprocess, 'run',
                               return_value=subprocess.CompletedProcess(
                                   [], 1, stdout='', stderr='')):
            self.assertFalse(self.hs._secrets_mount_details()[0])
        with mock.patch.object(self.hs.subprocess, 'run',
                               side_effect=[subprocess.CompletedProcess([], 0, stdout='', stderr=''),
                                            OSError('bad')]):
            self.assertEqual(self.hs._secrets_mount_details(), (True, None))
        with mock.patch.object(self.hs.os.path, 'exists', return_value=False):
            self.assertEqual(self.hs._secrets_access(False),
                             (False, '/secrets directory does not exist'))
        with mock.patch.object(self.hs.os.path, 'exists', return_value=True), \
                mock.patch.object(self.hs.os.path, 'isdir', return_value=True), \
                mock.patch.object(self.hs.os, 'listdir',
                                  side_effect=OSError('bad')):
            self.assertEqual(self.hs._secrets_access(True),
                             (False, 'Error accessing /secrets: bad'))
        with mock.patch.object(self.hs, '_secrets_mount_details',
                               return_value=(False, None)), \
                mock.patch.object(self.hs, '_secrets_access',
                                  return_value=(False, None)):
            self.assertEqual(self.hs.check_secrets_mount()['status'],
                             'unhealthy')

    def test_open_webui(self):
        response = mock.Mock(status_code=200, json=lambda: {'version': '2.0'})
        with mock.patch.object(self.hs.requests, 'get',
                               return_value=response):
            self.assertEqual(self.hs._open_webui_health_probe(),
                             (True, '2.0', None))
        with mock.patch.object(self.hs.requests, 'get',
                               side_effect=self.hs.requests.exceptions.Timeout):
            self.assertEqual(
                self.hs._open_webui_health_probe(),
                (False, None, 'Open-WebUI health check timeout'))
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=False), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=False):
            self.assertEqual(self.hs.check_open_webui()['status'], 'standby')
        with mock.patch.object(self.hs, 'check_service_status',
                               side_effect=RuntimeError('bad')):
            self.assertEqual(self.hs.check_open_webui()['status'], 'error')

    def test_dns_status_more_branches(self):
        resolver = mock.Mock()
        resolver.resolve.side_effect = self.hs.dns.resolver.NoAnswer
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            self.assertEqual(self.hs.check_dns_status()['status'], 'degraded')
        resolver.resolve.return_value = []
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            self.assertEqual(self.hs.check_dns_status()['status'], 'degraded')
        resolver.resolve.side_effect = RuntimeError('bad')
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            self.assertEqual(self.hs.check_dns_status()['status'], 'degraded')
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=False), \
                mock.patch.object(self.hs.dns.resolver, 'Resolver',
                                  return_value=resolver):
            self.assertEqual(self.hs.check_dns_status()['status'], 'unhealthy')

    def test_clock_skew_thresholds_and_errors(self):
        response = mock.Mock(offset=2, stratum=2, precision=1, delay=0.1)
        client = mock.Mock(request=mock.Mock(return_value=response))
        with mock.patch('ntplib.NTPClient', return_value=client), \
                mock.patch.object(self.hs, '_IS_STORAGE_NODE', False):
            self.assertEqual(self.hs.check_clock_skew()['status'], 'warning')
        response.offset = 20000
        with mock.patch('ntplib.NTPClient', return_value=client), \
                mock.patch.object(self.hs, '_IS_STORAGE_NODE', False):
            self.assertEqual(self.hs.check_clock_skew()['status'], 'critical')
        with mock.patch('ntplib.NTPClient', return_value=client), \
                mock.patch.object(self.hs, '_IS_STORAGE_NODE', False), \
                mock.patch.object(client, 'request',
                                  side_effect=socket.gaierror):
            self.assertEqual(self.hs.check_clock_skew()['status'], 'error')
        with mock.patch('ntplib.NTPClient', return_value=client), \
                mock.patch.object(self.hs, '_IS_STORAGE_NODE', False), \
                mock.patch.object(client, 'request',
                                  side_effect=OSError('bad')):
            self.assertEqual(self.hs.check_clock_skew()['status'], 'error')

    def test_registry_leader_and_split_brain(self):
        self.assertEqual(
            self.hs._registry_leader_status(True, True, False, None, 'bad'),
            ('degraded', 'Registry running but health check failed: bad'))
        self.assertEqual(
            self.hs._registry_leader_status(True, False, False, None, 'bad'),
            ('unhealthy',
             'Registry running but port not accessible: bad'))
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=False):
            self.assertEqual(self.hs.check_docker_registry()['status'],
                             'unhealthy')
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=True), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=True), \
                mock.patch.object(self.hs, '_registry_probe_status',
                                  return_value=(True, 'v2', None)):
            self.assertEqual(self.hs.check_docker_registry()['status'],
                             'healthy')

    def test_tang_service_paths(self):
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=True), \
                mock.patch.object(self.hs, '_tang_advertisement_status',
                                  return_value=(True, 2, None)):
            self.assertEqual(self.hs.check_tang_service()['status'], 'healthy')
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=False), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False):
            self.assertEqual(self.hs.check_tang_service()['status'],
                             'unhealthy')

    def test_secrets_mount_branches(self):
        with mock.patch.object(self.hs, '_secrets_mount_details',
                               return_value=(True, 'details')), \
                mock.patch.object(self.hs, '_secrets_access',
                                  return_value=(True, None)):
            self.assertEqual(self.hs.check_secrets_mount()['status'], 'healthy')
        with mock.patch.object(self.hs, '_secrets_mount_details',
                               return_value=(True, 'details')), \
                mock.patch.object(self.hs, '_secrets_access',
                                  return_value=(False, 'no access')), \
                mock.patch.object(self.hs, '_secrets_access',
                                  return_value=(False, 'no access')):
            self.assertEqual(self.hs.check_secrets_mount()['status'],
                             'degraded')

    def test_open_webui_service_paths(self):
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=True), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=True), \
                mock.patch.object(self.hs, '_open_webui_health_probe',
                                  return_value=(True, '2.0', None)):
            self.assertEqual(self.hs.check_open_webui()['status'], 'healthy')
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False), \
                mock.patch.object(self.hs, 'is_storage_leader',
                                  return_value=False):
            self.assertEqual(self.hs.check_open_webui()['status'], 'unhealthy')

    def test_probe_exception_paths(self):
        with mock.patch.object(self.hs.requests, 'get',
                               side_effect=OSError('bad')):
            self.assertEqual(
                self.hs._registry_probe_status()[2],
                'Registry health check failed: bad')
            self.assertEqual(
                self.hs._tang_advertisement_status()[2],
                'Tang advertisement failed: bad')
            self.assertEqual(
                self.hs._open_webui_health_probe()[2],
                'Open-WebUI health check failed: bad')
        response = mock.Mock(status_code=404, json=lambda: {})
        with mock.patch.object(self.hs.requests, 'get',
                               return_value=response):
            self.assertEqual(
                self.hs._open_webui_health_probe()[2],
                'Open-WebUI health check returned HTTP 404')
            self.assertEqual(self.hs._tang_advertisement_status()[2],
                             'Tang advertisement returned HTTP 404')
        with mock.patch.object(self.hs.requests, 'get',
                               return_value=mock.Mock(
                                   status_code=200,
                                   json=mock.Mock(side_effect=ValueError))):
            self.assertEqual(self.hs._tang_advertisement_status()[1], 'unknown')
            self.assertEqual(self.hs._open_webui_health_probe()[1], 'unknown')

    def test_error_paths(self):
        with mock.patch.object(self.hs, 'check_service_status',
                               return_value=True), \
                mock.patch.object(self.hs, 'check_port_open',
                                  return_value=False):
            self.assertEqual(self.hs.check_tang_service()['status'],
                             'unhealthy')
        with mock.patch.object(self.hs, '_secrets_mount_details',
                               side_effect=RuntimeError('bad')):
            self.assertEqual(self.hs.check_secrets_mount()['status'], 'error')
        with mock.patch.object(self.hs, '_secrets_mount_details',
                               side_effect=RuntimeError('bad')):
            self.assertEqual(self.hs.check_secrets_mount()['status'], 'error')


def ha_request_context():
    return get_health_api().app.test_request_context()


def _load_health_api():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'health-api.py')
    spec = importlib.util.spec_from_file_location('health_api_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_health_api():
    try:
        import health_api
        return health_api
    except ModuleNotFoundError:
        return _load_health_api()


if __name__ == '__main__':
    unittest.main()
