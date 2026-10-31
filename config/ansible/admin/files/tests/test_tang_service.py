"""Tests for the decomposed Tang service check."""

import unittest
from types import SimpleNamespace
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

import health_api as ha
import health_checks as hs
import health_common as hc
import health_report as hr
from probe_fakes import make_probe                    # noqa: E402


def tang_probe(http_get=None):
    return make_probe(
        services={'tang-server.service'}, ports={8777}, http_get=http_get)


class TangAdvertisementTests(unittest.TestCase):
    def test_200_with_key_count(self):
        response = SimpleNamespace(status_code=200,
                                   json=lambda: {'keys': [1, 2, 3]})
        healthy, keys, error = hs._tang_advertisement_status(
            tang_probe(lambda *args, **kwargs: response))
        self.assertTrue(healthy)
        self.assertEqual(keys, 3)
        self.assertIsNone(error)

    def test_200_with_unexpected_payload(self):
        response = SimpleNamespace(status_code=200, json=lambda: 'not a dict')
        healthy, keys, error = hs._tang_advertisement_status(
            tang_probe(lambda *args, **kwargs: response))
        self.assertTrue(healthy)
        self.assertEqual(keys, 'unknown')
        self.assertIsNone(error)

    def test_non_200(self):
        response = SimpleNamespace(status_code=500, json=lambda: {})
        healthy, keys, error = hs._tang_advertisement_status(
            tang_probe(lambda *args, **kwargs: response))
        self.assertFalse(healthy)
        self.assertIn('HTTP 500', error)

    def test_timeout(self):
        import requests
        def timeout_get(*args, **kwargs):
            raise requests.exceptions.Timeout()
        healthy, keys, error = hs._tang_advertisement_status(
            tang_probe(timeout_get))
        self.assertFalse(healthy)
        self.assertIn('timeout', error)


class TangHealthFromProbeTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = hs._tang_health_from_probe(True, True, True, 3, None)
        self.assertEqual(status, 'healthy')
        self.assertIn('3 keys', msg)

    def test_degraded(self):
        status, msg = hs._tang_health_from_probe(True, True, False, None, 'bad')
        self.assertEqual(status, 'degraded')
        self.assertIn('advertisement failed', msg)

    def test_port_closed(self):
        status, msg = hs._tang_health_from_probe(True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('port not accessible', msg)

    def test_not_running(self):
        status, msg = hs._tang_health_from_probe(False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('not running', msg)


class CheckTangServiceTests(unittest.TestCase):
    def test_error_returns_error_status(self):
        def failing_service(service):
            raise RuntimeError('boom')
        probe = hc.HealthProbeContext(
            is_etcd_node=True, is_storage_node=True,
            etcd_client=lambda: mock.Mock(),
            service_status=failing_service,
            port_open=lambda host, port, timeout=3: False,
            http_get=lambda *args, **kwargs: None,
            storage_leader=lambda: True,
            dhcp_leader=lambda: False,
            node_drained=lambda: False,
            core_node=lambda: True,
        )
        self.assertEqual(hs.check_tang_service(probe)['status'], 'error')



class RegistryHealthTests(unittest.TestCase):
    def test_leader_status(self):
        self.assertEqual(
            hs._registry_leader_status(True, True, True, 'v2', None),
            ('healthy', 'Registry running and healthy (API version v2)'),
        )
        self.assertEqual(
            hs._registry_leader_status(True, True, False, None, 'bad'),
            ('degraded', 'Registry running but health check failed: bad'),
        )
        self.assertEqual(
            hs._registry_leader_status(True, False, False, None, None),
            ('unhealthy', 'Registry running but port not accessible: None'),
        )
        self.assertEqual(
            hs._registry_leader_status(False, False, False, None, None),
            ('unhealthy', 'Registry not running'),
        )

    def test_probe_success(self):
        response = mock.Mock(status_code=200)
        response.headers = {'Docker-Distribution-Api-Version': 'v2'}
        with mock.patch.object(hs.requests, 'get', return_value=response):
            self.assertEqual(
                hs._registry_probe_status(),
                (True, 'v2', None),
            )

    def test_probe_error(self):
        with mock.patch.object(
            hs.requests, 'get', side_effect=hs.requests.exceptions.Timeout
        ):
            self.assertEqual(
                hs._registry_probe_status(),
                (False, None, 'Registry health check timeout'),
            )


class OpenWebuiHealthTests(unittest.TestCase):
    def test_leader_status(self):
        self.assertEqual(
            hs._open_webui_leader_status(True, True, True, '2.0', None),
            ('healthy', 'Open-WebUI running and healthy (version 2.0)'),
        )
        self.assertEqual(
            hs._open_webui_leader_status(True, True, False, None, 'bad'),
            ('degraded', 'Open-WebUI running but health check failed: bad'),
        )
        self.assertEqual(
            hs._open_webui_leader_status(True, False, False, None, None),
            ('unhealthy', 'Open-WebUI service running but port not accessible: None'),
        )
        self.assertEqual(
            hs._open_webui_leader_status(False, False, False, None, None),
            ('unhealthy', 'Open-WebUI service not running'),
        )

    def test_probe_success(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'version': '2.0'}
        with mock.patch.object(hs.requests, 'get', return_value=response):
            self.assertEqual(
                hs._open_webui_health_probe(),
                (True, '2.0', None),
            )

    def test_probe_error(self):
        with mock.patch.object(
            hs.requests, 'get', side_effect=hs.requests.exceptions.Timeout
        ):
            self.assertEqual(
                hs._open_webui_health_probe(),
                (False, None, 'Open-WebUI health check timeout'),
            )


if __name__ == '__main__':
    unittest.main()


class HealthApiLeaderTests(unittest.TestCase):
    def test_cached_leader_and_drain(self):
        with ha.app.test_request_context():
            with mock.patch.object(hc, '_get_etcd_client', return_value=mock.Mock()):
                hc.g._cache = {}
                hc.g._cache['is_storage_leader'] = True
                hc.g._cache['is_dhcp_leader'] = False
                hc.g._cache['is_node_drained'] = True
                self.assertTrue(hc.is_storage_leader())
                self.assertFalse(hc.is_dhcp_leader())
                self.assertTrue(hc.is_node_drained())

    def test_leader_results(self):
        cases = [
            ((b'other',), False),
            ((None,), False),
        ]
        for result, expected in cases:
            client = mock.Mock()
            client.get.return_value = result
            with ha.app.test_request_context():
                hc.g._cache = {}
                with mock.patch.object(hc, '_IS_ETCD_NODE', True), \
                     mock.patch.object(hc, '_HOSTNAME', 'test-node'), \
                     mock.patch.object(hc, '_get_etcd_client', return_value=client):
                    self.assertEqual(hc.is_storage_leader(), expected)
                    self.assertEqual(hc.is_dhcp_leader(), expected)
            client.get.assert_any_call('/cluster/leader/app')
            client.get.assert_any_call('/cluster/leader/dhcp')

    def test_drain_result(self):
        client = mock.Mock()
        client.get.return_value = (b'true',)
        with ha.app.test_request_context():
            hc.g._cache = {}
            with mock.patch.object(hc, '_IS_ETCD_NODE', True), \
                 mock.patch.object(hc, '_HOSTNAME', 'test-node'), \
                 mock.patch.object(hc, '_get_etcd_client', return_value=client):
                self.assertTrue(hc.is_node_drained())


class HealthApiServiceTests(unittest.TestCase):
    def test_service_status(self):
        for stdout, expected in [('active\n', True), ('inactive\n', False)]:
            result = mock.Mock(returncode=0, stdout=stdout, stderr='')
            with mock.patch.object(hc.subprocess, 'run', return_value=result):
                self.assertEqual(hc.check_service_status('svc'), expected)

    def test_port_open(self):
        with mock.patch.object(hc.socket, 'socket') as sock_cls:
            sock = sock_cls.return_value
            sock.connect_ex.return_value = 0
            self.assertTrue(hc.check_port_open('host', 80))
            sock.close.assert_called_once()

    def test_ceph_status(self):
        result = mock.Mock(returncode=0, stdout='HEALTH_OK\n', stderr='')
        with mock.patch.object(hc.subprocess, 'run', return_value=result):
            self.assertEqual(hs.check_ceph_status(), {'status': 'healthy', 'details': 'HEALTH_OK'})

    def test_vip_status(self):
        results = [
            mock.Mock(returncode=0, stdout='[{"ifname":"eth0"}]', stderr=''),
            mock.Mock(returncode=1, stdout='', stderr=''),
        ]
        with mock.patch.object(hc.subprocess, 'run', side_effect=results), \
             mock.patch.object(hc, 'check_service_status', return_value=True):
            status = hc.check_vip_status()
        self.assertTrue(status['gateway_vip']['active'])
        self.assertFalse(status['storage_vip']['active'])
        self.assertTrue(status['keepalived_service']['active'])

    def test_squid_health_unavailable(self):
        probe = make_probe()
        self.assertEqual(hr._squid_health(probe)['status'], 'unhealthy')

    def test_squid_health_functional(self):
        response = SimpleNamespace(status_code=200)
        probe = make_probe(
            services={'squid'}, ports={3128},
            http_get=lambda *args, **kwargs: response)
        self.assertEqual(hr._squid_health(probe)['status'], 'healthy')


class HealthApiDnsTests(unittest.TestCase):
    def test_dns_healthy(self):
        resolver = mock.Mock()
        answer = [mock.Mock(__str__=lambda self: 'ip-a')]
        resolver.resolve.return_value = answer
        with mock.patch.object(hc, '_read_resolv_nameserver',
                               return_value='127.0.0.1'), \
             mock.patch.object(hc, 'check_service_status',
                               side_effect=lambda name: name == 'dnsmasq'), \
             mock.patch.object(hc.dns.resolver, 'Resolver', return_value=resolver):
            result = hs.check_dns_status()
        self.assertEqual(result['status'], 'healthy')
        self.assertTrue(result['details']['dns_working'])

    def test_dns_timeout(self):
        resolver = mock.Mock()
        resolver.resolve.side_effect = hc.dns.resolver.Timeout
        with mock.patch.object(hc, '_read_resolv_nameserver',
                               return_value='127.0.0.1'), \
             mock.patch.object(hc, 'check_service_status',
                               side_effect=lambda name: name == 'dnsmasq'), \
             mock.patch.object(hc.dns.resolver, 'Resolver', return_value=resolver):
            result = hs.check_dns_status()
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
            return hs.check_clock_skew()

    def test_healthy(self):
        result = self._run(0.05)
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['details']['offset_ms'], 50)

    def test_critical_storage_node(self):
        with mock.patch.object(hs, '_IS_STORAGE_NODE', True):
            self.assertEqual(self._run(1.5)['status'], 'critical')

    def test_error(self):
        import ntplib
        self.assertEqual(self._run(0, exception=ntplib.NTPException('bad'))['status'], 'error')


class HealthApiDockerTests(unittest.TestCase):
    def test_docker_daemon(self):
        with mock.patch.object(hs, 'check_service_status', return_value=True):
            self.assertEqual(hs.check_docker_daemon()['status'], 'healthy')
        with mock.patch.object(hs, 'check_service_status', return_value=False):
            self.assertEqual(hs.check_docker_daemon()['status'], 'unhealthy')


class SecretsMountTests(unittest.TestCase):
    def test_mount_details(self):
        results = [
            mock.Mock(returncode=0, stdout='', stderr=''),
            mock.Mock(returncode=0, stdout='source:fstype:opts', stderr=''),
        ]
        with mock.patch.object(hs.subprocess, 'run', side_effect=results):
            self.assertEqual(hs._secrets_mount_details(), (True, 'source:fstype:opts'))

    def test_mount_details_not_mounted(self):
        result = mock.Mock(returncode=1, stdout='', stderr='')
        with mock.patch.object(hs.subprocess, 'run', return_value=result):
            self.assertEqual(hs._secrets_mount_details(), (False, None))

    def test_secrets_access_ok(self):
        with mock.patch.object(hs.os.path, 'exists', return_value=True), \
             mock.patch.object(hs.os.path, 'isdir', return_value=True), \
             mock.patch.object(hs.os, 'listdir', return_value=['a']):
            self.assertEqual(hs._secrets_access(True), (True, None))

    def test_secrets_access_permission_treated_as_mounted(self):
        with mock.patch.object(hs.os.path, 'exists', return_value=True), \
             mock.patch.object(hs.os.path, 'isdir', return_value=True), \
             mock.patch.object(hs.os, 'listdir', side_effect=PermissionError):
            self.assertEqual(hs._secrets_access(True), (True, None))


class ServiceMetricValueTests(unittest.TestCase):
    def test_ordinals(self):
        self.assertEqual(hr.service_metric_value('healthy'), 0)
        self.assertEqual(hr.service_metric_value('degraded'), 1)
        self.assertEqual(hr.service_metric_value('unhealthy'), 2)
        self.assertEqual(hr.service_metric_value('standby'), 3)
        self.assertEqual(hr.service_metric_value('unknown'), 2)
