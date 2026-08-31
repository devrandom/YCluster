"""Unit tests for app.py comprehensive-health decomposition."""

import contextlib
import json as _json
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


class MacFromNeighborTableTests(unittest.TestCase):
    def _run(self, stdout='[]'):
        with mock.patch.object(appmod, 'subprocess') as sp:
            sp.run.return_value = mock.Mock(returncode=0, stdout=stdout)
            result = appmod._mac_from_neighbor_table('ip-a')
        return result

    def test_valid_mac(self):
        stdout = '[{"dst": "ip-a", "lladdr": "aa:bb:cc:dd:ee:ff"}]'
        self.assertEqual(self._run(stdout), 'aa:bb:cc:dd:ee:ff')

    def test_invalid_mac_format(self):
        stdout = '[{"dst": "ip-a", "lladdr": "short"}]'
        self.assertIsNone(self._run(stdout))

    def test_no_match(self):
        stdout = '[{"dst": "ip-b", "lladdr": "aa:bb:cc:dd:ee:ff"}]'
        self.assertIsNone(self._run(stdout))

    def test_bad_json(self):
        self.assertIsNone(self._run('not json'))


class MacFromDHCPLeasesTests(unittest.TestCase):
    def test_finds_matching_lease(self):
        import json as _json
        client = mock.Mock()
        lease = _json.dumps({'ip': 'ip-a', 'mac': 'aa:bb:cc:dd:ee:ff'})
        client.get_prefix.return_value = [(lease.encode(), None)]
        self.assertEqual(appmod._mac_from_dhcp_leases(client, 'ip-a'),
                         'aa:bb:cc:dd:ee:ff')

    def test_no_match(self):
        import json as _json
        client = mock.Mock()
        lease = _json.dumps({'ip': 'ip-b', 'mac': 'aa:bb:cc:dd:ee:ff'})
        client.get_prefix.return_value = [(lease.encode(), None)]
        self.assertIsNone(appmod._mac_from_dhcp_leases(client, 'ip-a'))

    def test_invalid_json_skipped(self):
        client = mock.Mock()
        client.get_prefix.return_value = [(b'not json', None)]
        self.assertIsNone(appmod._mac_from_dhcp_leases(client, 'ip-a'))


class GetMacFromIPTTests(unittest.TestCase):
    def test_empty_ip_returns_none(self):
        with mock.patch.object(appmod, 'get_etcd_client') as _:
            self.assertIsNone(appmod.get_mac_from_ip(None))

    def test_dhcp_hit_skips_neighbor(self):
        with mock.patch.object(appmod, 'get_etcd_client'), \
             mock.patch.object(appmod, '_mac_from_dhcp_leases',
                               return_value='aa:bb:cc:dd:ee:ff') as dhcp, \
             mock.patch.object(appmod, '_mac_from_neighbor_table') as neighbor:
            self.assertEqual(appmod.get_mac_from_ip('ip-a'),
                             'aa:bb:cc:dd:ee:ff')
        neighbor.assert_not_called()


class OpenWebuiLeaderStatusTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = appmod._open_webui_leader_status(
            True, True, True, '1.0', None)
        self.assertEqual(status, 'healthy')
        self.assertIn('version 1.0', msg)

    def test_degraded(self):
        status, msg = appmod._open_webui_leader_status(
            True, True, False, None, 'HTTP 503')
        self.assertEqual(status, 'degraded')
        self.assertIn('health check failed', msg)

    def test_port_closed(self):
        status, msg = appmod._open_webui_leader_status(
            True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('port not accessible', msg)

    def test_not_running(self):
        status, msg = appmod._open_webui_leader_status(
            False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('not running', msg)


class OpenWebuiProbeTests(unittest.TestCase):
    def test_200_with_version(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'version': '2.0'}
        with mock.patch.object(appmod.requests, 'get', return_value=response):
            healthy, version, error = appmod._open_webui_health_probe()
        self.assertTrue(healthy)
        self.assertEqual(version, '2.0')
        self.assertIsNone(error)

    def test_non_200(self):
        response = mock.Mock(status_code=503)
        with mock.patch.object(appmod.requests, 'get', return_value=response):
            healthy, version, error = appmod._open_webui_health_probe()
        self.assertFalse(healthy)
        self.assertIn('HTTP 503', error)


class RegistryLeaderStatusTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = appmod._registry_leader_status(True, True, True, 'v2', None)
        self.assertEqual(status, 'healthy')
        self.assertIn('API version v2', msg)

    def test_degraded(self):
        status, msg = appmod._registry_leader_status(True, True, False, None, 'bad')
        self.assertEqual(status, 'degraded')
        self.assertIn('health check failed', msg)

    def test_port_closed(self):
        status, msg = appmod._registry_leader_status(True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('port not accessible', msg)

    def test_not_running(self):
        status, msg = appmod._registry_leader_status(False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('not running', msg)


class NodeHostsEntriesTests(unittest.TestCase):
    def _client(self, allocations):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (_json.dumps(a).encode(), None) for a in allocations]
        return client

    @mock.patch.object(appmod, 'determine_ip_from_hostname')
    def test_regular_node_gets_amt(self, determine):
        determine.return_value = 'ip-amt'
        client = self._client([{'hostname': 'n1', 'ip': 'ip-n1'}])
        entries = appmod._node_hosts_entries(client)
        self.assertEqual(sorted(entries),
                         ['ip-amt n1a n1a.xc', 'ip-n1 n1 n1.xc'])

    @mock.patch.object(appmod, 'determine_ip_from_hostname',
                       return_value='ip-amt')
    def test_amt_hostname_skipped(self, determine):
        client = self._client([{'hostname': 'n1a', 'ip': 'ip-amt'}])
        self.assertEqual(appmod._node_hosts_entries(client), [])

    @mock.patch.object(appmod, 'determine_ip_from_hostname')
    def test_bad_json_skipped(self, determine):
        client = mock.Mock()
        client.get_prefix.return_value = [(b'not json', None)]
        self.assertEqual(appmod._node_hosts_entries(client), [])


class FrontendHostsEntriesTests(unittest.TestCase):
    def test_named_and_ip(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (_json.dumps({'name': 'fe1', 'ip': 'ip-fe1'}).encode(), None)]
        self.assertEqual(appmod._frontend_hosts_entries(client),
                         ['ip-fe1 fe1 fe1.xc'])

    def test_missing_name_or_ip(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (_json.dumps({'name': 'fe1'}).encode(), None),
            (_json.dumps({'ip': 'ip-fe1'}).encode(), None)]
        self.assertEqual(appmod._frontend_hosts_entries(client), [])
