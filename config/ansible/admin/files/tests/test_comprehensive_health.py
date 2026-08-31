"""Unit tests for app.py comprehensive-health decomposition."""

import contextlib
import unittest
from unittest import mock

import app as appmod


def service(status, **details):
    return {'status': status, 'details': details or {}}


class AddHealthServiceTests(unittest.TestCase):
    def test_unhealthy_and_degraded_rules(self):
        health = {'overall': 'healthy', 'services': {}}
        appmod._add_health_service(health, 'a', service('unhealthy'))
        self.assertEqual(health['overall'], 'unhealthy')

        health = {'overall': 'healthy', 'services': {}}
        appmod._add_health_service(health, 'b', service('critical'), ('critical',), ('warning',))
        self.assertEqual(health['overall'], 'unhealthy')

        health = {'overall': 'healthy', 'services': {}}
        appmod._add_health_service(health, 'c', service('warning'), ('critical',), ('warning',))
        self.assertEqual(health['overall'], 'degraded')

        health = {'overall': 'degraded', 'services': {}}
        appmod._add_health_service(health, 'd', service('degraded'))
        self.assertEqual(health['overall'], 'degraded')

    def test_ceph_rule_treats_non_healthy_or_degraded_as_unhealthy(self):
        health = {'overall': 'healthy', 'services': {}}
        appmod._add_health_service(
            health, 'ceph', service('unknown'),
            unhealthy_statuses=('unhealthy', 'error', 'standby', 'unknown'),
            degraded_statuses=(),
        )
        self.assertEqual(health['overall'], 'unhealthy')


class ServiceRuleTests(unittest.TestCase):
    def test_required_service(self):
        got = appmod._required_service_health(True, True, 'postgresql', 'storage leader')
        self.assertEqual(got['status'], 'healthy')
        self.assertEqual(got['details']['reason'], 'storage leader')
        self.assertTrue(got['details']['required'])

    def test_standby_and_split_brain(self):
        got = appmod._standby_service_health(False, False, 'not storage leader')
        self.assertEqual(got['status'], 'standby')
        self.assertEqual(got['details']['reason'], 'not storage leader')

        got = appmod._standby_service_health(False, True, 'not storage leader')
        self.assertEqual(got['status'], 'unhealthy')
        self.assertEqual(got['details']['reason'], 'split-brain: running on non-leader')


class HealthHelpersTests(unittest.TestCase):
    def test_etcd_paths(self):
        with mock.patch.object(appmod, 'is_etcd_node', return_value=False):
            self.assertEqual(appmod._etcd_health()['status'], 'not_applicable')

        client = mock.Mock()
        with mock.patch.object(appmod, 'is_etcd_node', return_value=True), \
             mock.patch.object(appmod, 'get_etcd_client', return_value=client):
            self.assertEqual(appmod._etcd_health()['status'], 'healthy')

        client.get.side_effect = RuntimeError('down')
        with mock.patch.object(appmod, 'is_etcd_node', return_value=True), \
             mock.patch.object(appmod, 'get_etcd_client', return_value=client):
            self.assertEqual(appmod._etcd_health()['status'], 'unhealthy')

    def test_storage_only_helpers(self):
        not_applicable = {'status': 'not_applicable'}
        with mock.patch.object(appmod, 'check_ceph_status') as check:
            self.assertEqual(appmod._ceph_health(False), {'status': 'not_applicable', 'details': {'reason': 'not required on compute nodes'}})
            check.assert_not_called()

        with mock.patch.object(appmod, 'check_ceph_status', return_value=not_applicable):
            self.assertIs(appmod._ceph_health(True), not_applicable)

    def test_keepalived_paths(self):
        with mock.patch.object(appmod, 'platform') as platform, \
             mock.patch.object(appmod, 'is_etcd_node', return_value=True), \
             mock.patch.object(appmod, 'get_core_nodes', return_value=['node-a']), \
             mock.patch.object(appmod, 'check_service_status', return_value=True):
            platform.node.return_value = 'node-a'
            self.assertEqual(appmod._keepalived_health()['status'], 'healthy')

        with mock.patch.object(appmod, 'platform') as platform, \
             mock.patch.object(appmod, 'is_etcd_node', return_value=False), \
             mock.patch.object(appmod, 'get_core_nodes', side_effect=AssertionError('should not call')), \
             mock.patch.object(appmod, 'check_service_status', return_value=False):
            platform.node.return_value = 'node-b'
            self.assertEqual(appmod._keepalived_health()['status'], 'standby')

    def test_squid_paths(self):
        with mock.patch.object(appmod, 'check_service_status', return_value=False), \
             mock.patch.object(appmod, 'check_port_open', return_value=False):
            self.assertEqual(appmod._squid_health()['status'], 'unhealthy')


class ComprehensiveHealthTests(unittest.TestCase):
    def test_all_healthy_storage_leader(self):
        vip = {
            'gateway_vip': {'active': True, 'ip': 'gateway'},
            'storage_vip': {'active': True, 'ip': 'storage'},
        }
        patches = {
            'get_current_node_type': 'storage',
            'is_etcd_node': True,
            'is_storage_leader': True,
            'is_dhcp_leader': True,
            'is_node_drained': False,
            'check_dns_status': service('healthy'),
            'check_certificate_expiry': service('healthy'),
            'check_clock_skew': service('healthy'),
            'check_docker_registry': service('healthy'),
            'check_open_webui': service('healthy'),
            'check_vip_status': vip,
        }
        functions = {
            'get_etcd_client': mock.Mock(),
            'check_ceph_status': service('healthy'),
            'check_service_status': True,
            'check_port_open': True,
            'check_docker_daemon': service('healthy'),
            'check_tang_service': service('healthy'),
            'check_secrets_mount': service('healthy'),
            'get_core_nodes': ['node-a'],
        }
        patched = []
        for name, value in patches.items():
            patched.append(mock.patch.object(appmod, name, return_value=value))
        platform = mock.Mock()
        platform.node.return_value = 'node-a'
        patched.append(mock.patch.object(appmod, 'platform', new=platform))
        response = mock.Mock(status_code=200)
        patched.append(mock.patch.object(appmod.requests, 'get', return_value=response))
        for name, value in functions.items():
            patched.append(mock.patch.object(appmod, name, return_value=value))
        with contextlib.ExitStack() as stack:
            for item in patched:
                stack.enter_context(item)
                # keep reference?
            health = appmod.get_comprehensive_health()
        self.assertEqual(health['overall'], 'healthy')
        self.assertEqual(health['storage_leader'], True)
        self.assertEqual(health['dhcp_leader'], True)
        self.assertEqual(health['drained'], False)
        expected = [
            'etcd', 'ceph', 'postgresql', 'qdrant', 'storage_leader_election',
            'dhcp', 'dns', 'squid', 'ntp', 'tls_certificate', 'clock_skew',
            'docker_daemon', 'docker_registry', 'tang', 'secrets_mount',
            'open_webui', 'gateway_vip', 'storage_vip', 'keepalived'
        ]
        self.assertEqual(list(health['services']), expected)

    def test_unhealthy_service_sets_overall(self):
        vip = {'gateway_vip': {'active': False}, 'storage_vip': {'active': False}}
        patches = {
            'get_current_node_type': lambda: 'compute',
            'is_etcd_node': lambda: False,
            'is_storage_leader': lambda: False,
            'is_dhcp_leader': lambda: False,
            'is_node_drained': lambda: True,
            'check_ceph_status': lambda: service('not_applicable'),
            'check_service_status': lambda name: False,
            'check_port_open': lambda *args: False,
            'check_dns_status': lambda: service('unhealthy'),
            'check_certificate_expiry': lambda: service('healthy'),
            'check_clock_skew': lambda: service('healthy'),
            'check_docker_daemon': lambda: service('not_applicable'),
            'check_docker_registry': lambda: service('standby'),
            'check_tang_service': lambda: service('not_applicable'),
            'check_secrets_mount': lambda: service('not_applicable'),
            'check_open_webui': lambda: service('healthy'),
            'check_vip_status': lambda: vip,
            'get_core_nodes': lambda: [],
        }
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(appmod, name, side_effect=value))
            health = appmod.get_comprehensive_health()
        self.assertEqual(health['overall'], 'unhealthy')


if __name__ == '__main__':
    unittest.main()
