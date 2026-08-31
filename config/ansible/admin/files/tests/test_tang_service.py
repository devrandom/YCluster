"""Tests for the decomposed Tang service check."""

import unittest
from unittest import mock

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for _p in (_HERE, _FILES, os.path.join(_FILES, 'ycluster')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_API_PATH = os.path.join(_HERE, '..', 'health-api.py')
_spec = importlib.util.spec_from_file_location('health_api', _API_PATH)
ha = importlib.util.module_from_spec(_spec)
sys.modules['health_api'] = ha
_spec.loader.exec_module(ha)

import app as appmod
import health_api as ha


class TangAdvertisementTests(unittest.TestCase):
    def test_200_with_key_count(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'keys': [1, 2, 3]}
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertTrue(healthy)
        self.assertEqual(keys, 3)
        self.assertIsNone(error)

    def test_200_with_unexpected_payload(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = 'not a dict'
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertTrue(healthy)
        self.assertEqual(keys, 'unknown')
        self.assertIsNone(error)

    def test_non_200(self):
        response = mock.Mock(status_code=500)
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('HTTP 500', error)

    def test_timeout(self):
        import requests
        with mock.patch.object(ha.requests, 'get',
                               side_effect=requests.exceptions.Timeout()):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('timeout', error)


class TangHealthFromProbeTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = ha._tang_health_from_probe(True, True, True, 3, None)
        self.assertEqual(status, 'healthy')
        self.assertIn('3 keys', msg)

    def test_degraded(self):
        status, msg = ha._tang_health_from_probe(True, True, False, None, 'bad')
        self.assertEqual(status, 'degraded')
        self.assertIn('advertisement failed', msg)

    def test_port_closed(self):
        status, msg = ha._tang_health_from_probe(True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('port not accessible', msg)

    def test_not_running(self):
        status, msg = ha._tang_health_from_probe(False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('not running', msg)


class CheckTangServiceTests(unittest.TestCase):
    def test_error_returns_error_status(self):
        with mock.patch.object(ha, 'check_service_status',
                               side_effect=RuntimeError('boom')):
            result = ha.check_tang_service()
        self.assertEqual(result['status'], 'error')



class RegistryHealthTests(unittest.TestCase):
    def test_leader_status(self):
        self.assertEqual(
            ha._registry_leader_status(True, True, True, 'v2', None),
            ('healthy', 'Registry running and healthy (API version v2)'),
        )
        self.assertEqual(
            ha._registry_leader_status(True, True, False, None, 'bad'),
            ('degraded', 'Registry running but health check failed: bad'),
        )
        self.assertEqual(
            ha._registry_leader_status(True, False, False, None, None),
            ('unhealthy', 'Registry running but port not accessible: None'),
        )
        self.assertEqual(
            ha._registry_leader_status(False, False, False, None, None),
            ('unhealthy', 'Registry not running'),
        )

    def test_probe_success(self):
        response = mock.Mock(status_code=200)
        response.headers = {'Docker-Distribution-Api-Version': 'v2'}
        with mock.patch.object(ha.requests, 'get', return_value=response):
            self.assertEqual(
                ha._registry_probe_status(),
                (True, 'v2', None),
            )

    def test_probe_error(self):
        with mock.patch.object(
            ha.requests, 'get', side_effect=ha.requests.exceptions.Timeout
        ):
            self.assertEqual(
                ha._registry_probe_status(),
                (False, None, 'Registry health check timeout'),
            )


class OpenWebuiHealthTests(unittest.TestCase):
    def test_leader_status(self):
        self.assertEqual(
            ha._open_webui_leader_status(True, True, True, '2.0', None),
            ('healthy', 'Open-WebUI running and healthy (version 2.0)'),
        )
        self.assertEqual(
            ha._open_webui_leader_status(True, True, False, None, 'bad'),
            ('degraded', 'Open-WebUI running but health check failed: bad'),
        )
        self.assertEqual(
            ha._open_webui_leader_status(True, False, False, None, None),
            ('unhealthy', 'Open-WebUI service running but port not accessible: None'),
        )
        self.assertEqual(
            ha._open_webui_leader_status(False, False, False, None, None),
            ('unhealthy', 'Open-WebUI service not running'),
        )

    def test_probe_success(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'version': '2.0'}
        with mock.patch.object(ha.requests, 'get', return_value=response):
            self.assertEqual(
                ha._open_webui_health_probe(),
                (True, '2.0', None),
            )

    def test_probe_error(self):
        with mock.patch.object(
            ha.requests, 'get', side_effect=ha.requests.exceptions.Timeout
        ):
            self.assertEqual(
                ha._open_webui_health_probe(),
                (False, None, 'Open-WebUI health check timeout'),
            )


if __name__ == '__main__':
    unittest.main()
if __name__ == '__main__':
    unittest.main()


class AppTangAdvertisementTests(unittest.TestCase):
    def test_200_with_key_count(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'keys': [1, 2, 3]}
        with mock.patch.object(appmod.requests, 'get', return_value=response):
            healthy, keys, error = appmod._tang_advertisement_status()
        self.assertTrue(healthy)
        self.assertEqual(keys, 3)
        self.assertIsNone(error)

    def test_non_200(self):
        response = mock.Mock(status_code=500)
        with mock.patch.object(appmod.requests, 'get', return_value=response):
            healthy, keys, error = appmod._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('HTTP 500', error)

    def test_timeout(self):
        import requests
        with mock.patch.object(appmod.requests, 'get',
                               side_effect=requests.exceptions.Timeout()):
            healthy, keys, error = appmod._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('timeout', error)


class AppTangHealthFromProbeTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = appmod._tang_health_from_probe(True, True, True, 3, None)
        self.assertEqual(status, 'healthy')

    def test_degraded(self):
        status, msg = appmod._tang_health_from_probe(True, True, False, None, 'bad')
        self.assertEqual(status, 'degraded')

    def test_port_closed(self):
        status, msg = appmod._tang_health_from_probe(True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')

    def test_not_running(self):
        status, msg = appmod._tang_health_from_probe(False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')


class HealthApiLeaderTests(unittest.TestCase):
    def test_cached_leader_and_drain(self):
        with ha.app.test_request_context():
            with mock.patch.object(ha, '_get_etcd_client', return_value=mock.Mock()):
                ha.g._cache = {}
                ha.g._cache['is_storage_leader'] = True
                ha.g._cache['is_dhcp_leader'] = False
                ha.g._cache['is_node_drained'] = True
                self.assertTrue(ha.is_storage_leader())
                self.assertFalse(ha.is_dhcp_leader())
                self.assertTrue(ha.is_node_drained())

    def test_leader_results(self):
        cases = [
            ((b'other',), False),
            ((None,), False),
        ]
        for result, expected in cases:
            client = mock.Mock()
            client.get.return_value = result
            with ha.app.test_request_context():
                ha.g._cache = {}
                with mock.patch.object(ha, '_IS_ETCD_NODE', True), \
                     mock.patch.object(ha, '_HOSTNAME', 'test-node'), \
                     mock.patch.object(ha, '_get_etcd_client', return_value=client):
                    self.assertEqual(ha.is_storage_leader(), expected)
                    self.assertEqual(ha.is_dhcp_leader(), expected)
            client.get.assert_any_call('/cluster/leader/app')
            client.get.assert_any_call('/cluster/leader/dhcp')

    def test_drain_result(self):
        client = mock.Mock()
        client.get.return_value = (b'true',)
        with ha.app.test_request_context():
            ha.g._cache = {}
            with mock.patch.object(ha, '_IS_ETCD_NODE', True), \
                 mock.patch.object(ha, '_HOSTNAME', 'test-node'), \
                 mock.patch.object(ha, '_get_etcd_client', return_value=client):
                self.assertTrue(ha.is_node_drained())


class HealthApiServiceTests(unittest.TestCase):
    def test_service_status(self):
        for stdout, expected in [('active\n', True), ('inactive\n', False)]:
            result = mock.Mock(returncode=0, stdout=stdout, stderr='')
            with mock.patch.object(ha.subprocess, 'run', return_value=result):
                self.assertEqual(ha.check_service_status('svc'), expected)

    def test_port_open(self):
        with mock.patch.object(ha.socket, 'socket') as sock_cls:
            sock = sock_cls.return_value
            sock.connect_ex.return_value = 0
            self.assertTrue(ha.check_port_open('host', 80))
            sock.close.assert_called_once()

    def test_ceph_status(self):
        result = mock.Mock(returncode=0, stdout='HEALTH_OK\n', stderr='')
        with mock.patch.object(ha.subprocess, 'run', return_value=result):
            self.assertEqual(ha.check_ceph_status(), {'status': 'healthy', 'details': 'HEALTH_OK'})

    def test_vip_status(self):
        results = [
            mock.Mock(returncode=0, stdout='[{"ifname":"eth0"}]', stderr=''),
            mock.Mock(returncode=1, stdout='', stderr=''),
        ]
        with mock.patch.object(ha.subprocess, 'run', side_effect=results), \
             mock.patch.object(ha, 'check_service_status', return_value=True):
            status = ha.check_vip_status()
        self.assertTrue(status['gateway_vip']['active'])
        self.assertFalse(status['storage_vip']['active'])
        self.assertTrue(status['keepalived_service']['active'])

    def test_squid_health_unavailable(self):
        with mock.patch.object(ha, 'check_service_status', return_value=False), \
             mock.patch.object(ha, 'check_port_open', return_value=False):
            self.assertEqual(ha._squid_health()['status'], 'unhealthy')

    def test_squid_health_functional(self):
        response = mock.Mock(status_code=200)
        with mock.patch.object(ha, 'check_service_status', return_value=True), \
             mock.patch.object(ha, 'check_port_open', return_value=True), \
             mock.patch.object(ha.requests, 'get', return_value=response):
            self.assertEqual(ha._squid_health()['status'], 'healthy')


class HealthApiDnsTests(unittest.TestCase):
    def test_dns_healthy(self):
        resolver = mock.Mock()
        answer = [mock.Mock(__str__=lambda self: 'ip-a')]
        resolver.resolve.return_value = answer
        with mock.patch.object(ha, 'check_service_status', return_value=True), \
             mock.patch.object(ha.dns.resolver, 'Resolver', return_value=resolver):
            result = ha.check_dns_status()
        self.assertEqual(result['status'], 'healthy')
        self.assertTrue(result['details']['dns_working'])

    def test_dns_timeout(self):
        resolver = mock.Mock()
        resolver.resolve.side_effect = ha.dns.resolver.Timeout
        with mock.patch.object(ha, 'check_service_status', return_value=True), \
             mock.patch.object(ha.dns.resolver, 'Resolver', return_value=resolver):
            result = ha.check_dns_status()
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['details']['message'], 'Local DNS query timeout')


class HealthApiClockSkewTests(unittest.TestCase):
    def _run(self, offset, exception=None):
        response = mock.Mock(offset=offset, stratum=2, precision=1,
                             delay=0.1)
        client = mock.Mock()
        client.request.return_value = response
        with mock.patch('ntplib.NTPClient', return_value=client):
            if exception:
                client.request.side_effect = exception
            return ha.check_clock_skew()

    def test_healthy(self):
        result = self._run(0.05)
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['details']['offset_ms'], 50)

    def test_critical_storage_node(self):
        with mock.patch.object(ha, '_IS_STORAGE_NODE', True):
            self.assertEqual(self._run(1.5)['status'], 'critical')

    def test_error(self):
        import ntplib
        self.assertEqual(self._run(0, exception=ntplib.NTPException('bad'))['status'], 'error')


class HealthApiDockerTests(unittest.TestCase):
    def test_docker_daemon(self):
        with mock.patch.object(ha, 'check_service_status', return_value=True):
            self.assertEqual(ha.check_docker_daemon()['status'], 'healthy')
        with mock.patch.object(ha, 'check_service_status', return_value=False):
            self.assertEqual(ha.check_docker_daemon()['status'], 'unhealthy')
