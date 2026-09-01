"""Tests for cluster health collection and rendered subsystem reports."""

import io
import json
import socket
import sys
import types
import unittest
import urllib.error
import dns.resolver as dns_resolver
from contextlib import redirect_stdout
from unittest import mock

from ycluster.utils import check_cluster as cc
from ycluster.utils import check_cluster_views as views


def render(function, *args, **kwargs):
    output = io.StringIO()
    with redirect_stdout(output):
        function(*args, **kwargs)
    return output.getvalue()


class EtcdCollectionTests(unittest.TestCase):
    def test_get_cluster_members_uses_first_client_url(self):
        payload = json.dumps({'members': [
            {'clientURLs': ['https://host-a:2379', 'bad']},
            {'clientURLs': []},
        ]})
        result = mock.Mock(returncode=0, stdout=payload)
        with mock.patch.object(cc, 'get_etcd_hosts', return_value=['host-a:2379']), \
             mock.patch.object(cc.subprocess, 'run', return_value=result):
            self.assertEqual(cc.get_cluster_members('initial'), ['host-a:2379'])

    def test_get_cluster_members_falls_back_on_error(self):
        with mock.patch.object(cc.subprocess, 'run', side_effect=OSError('boom')):
            self.assertEqual(cc.get_cluster_members('initial'), ['initial'])

    def test_check_etcd_host_returns_leader_and_status(self):
        members = [
            mock.Mock(id=1, client_urls=['https://host-a:2379']),
            mock.Mock(id=2, client_urls=['https://host-a:2379']),
        ]
        status = mock.Mock(version='3.5', db_size=1000, leader=members[0], raft_index=7)
        client = mock.Mock(status=lambda: status, members=members)
        with mock.patch.object(cc.etcd3, 'client', return_value=client):
            result = cc.check_etcd_host('host-a:2379')
        self.assertTrue(result['healthy'])
        self.assertTrue(result['is_leader'])
        self.assertEqual(result['member_count'], 2)

    def test_check_etcd_host_returns_failure(self):
        with mock.patch.object(cc.etcd3, 'client', side_effect=OSError('down')):
            result = cc.check_etcd_host('host-a:2379')
        self.assertFalse(result['healthy'])
        self.assertEqual(result['error'], 'down')

    def test_get_all_nodes_skips_invalid_json(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (b'{"hostname": "ok", "ip": "ip-a"}', None),
            (b'broken', None),
        ]
        self.assertEqual(cc.get_all_nodes(client)[0]['hostname'], 'ok')

    def test_fetch_failure_is_reported(self):
        with mock.patch.object(cc.urllib.request, 'urlopen', side_effect=OSError('no route')):
            self.assertEqual(cc.fetch_cluster_status(), {'_error': 'no route'})

class ServiceCheckTests(unittest.TestCase):
    def test_dhcp_health_endpoint(self):
        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.read = lambda: json.dumps({'status': 'healthy', 'etcd_connected': True}).encode()
        with mock.patch.object(cc.urllib.request, 'urlopen', return_value=response):
            result = cc.check_dhcp_service_on_host('ip-a')
        self.assertTrue(result['running'])

    def test_dhcp_error_paths(self):
        errors = [
            urllib.error.HTTPError('u', 404, 'missing', {}, None),
            urllib.error.URLError('no route'),
            socket.timeout(),
            ValueError('unexpected'),
        ]
        for error in errors:
            with mock.patch.object(cc.urllib.request, 'urlopen', side_effect=error):
                result = cc.check_dhcp_service_on_host('ip-a')
            self.assertFalse(result['running'])

    def test_dns_missing_dependency(self):
        with mock.patch.dict(sys.modules, {'dns': None, 'dns.resolver': None}):
            result = cc.check_dnsmasq_service_on_host('ip-a', 's1')
        self.assertFalse(result['running'])
        self.assertEqual(result['error'], 'dnspython not installed')

    def test_dns_nxdomain_and_bad_subnet(self):
        resolver = mock.Mock()
        module = mock.Mock()
        module.resolver.NXDOMAIN = dns_resolver.NXDOMAIN
        module.resolver.NoNameservers = dns_resolver.NoNameservers
        resolver.resolve.side_effect = dns_resolver.NXDOMAIN('missing')
        module.resolver.Resolver.return_value = resolver
        with mock.patch.dict(sys.modules, {'dns': module, 'dns.resolver': module}):
            result = cc.check_dnsmasq_service_on_host('ip-a', 's1')
        self.assertTrue(result['running'])
        self.assertFalse(result['dns_working'])


class ViewHelperTests(unittest.TestCase):
    def test_check_etcd_hosts_marks_and_collects(self):
        marks = []
        timer = lambda label: marks.append(label)
        results = [{'healthy': True}, {'healthy': False}]
        with mock.patch.object(views, 'get_cluster_members', return_value=['a', 'b']), \
             mock.patch.object(views, 'check_etcd_host', side_effect=results):
            returned_hosts, returned_results = views._check_etcd_hosts(timer)
        self.assertEqual(returned_hosts, ['a', 'b'])
        self.assertEqual(returned_results, results)
        self.assertEqual(marks, ['get_cluster_members', 'etcd_health_checks'])

    def test_get_node_lists(self):
        with mock.patch.object(views, 'get_all_nodes', return_value=[]):
            self.assertEqual(views._get_node_lists(None), ([], []))
        nodes = [
            {'hostname': 's1', 'ip': 'ip-a'},
            {'hostname': 's1a', 'ip': 'ip-b'},
            {'hostname': 's2'},
        ]
        with mock.patch.object(views, 'get_all_nodes', return_value=nodes):
            all_nodes, non_amt = views._get_node_lists(object())
        self.assertEqual(all_nodes, nodes)
        self.assertEqual(non_amt, [nodes[0]])

    def test_compact_etcd(self):
        text = render(views._print_compact_etcd, [
            {'healthy': True, 'host': 'a', 'is_leader': True},
            {'healthy': False, 'host': 'b', 'error': 'down'},
        ], [])
        self.assertIn('1/2 healthy', text)

    def test_compact_vip_storage_branches(self):
        problems = []
        views._print_compact_vip_and_storage({'_error': 'timeout'}, problems)
        self.assertEqual(problems, [f'  vip: storage VIP ({views.STORAGE_VIP}) unreachable: timeout'])

        status = {
            'vipStatus': {
                'gateway_vip': {'master_hostname': '?', 'ip': 'ip-a'},
                'storage_vip': {'master_hostname': '?', 'ip': 'ip-b'},
            },
            'leadership': {},
        }
        views._print_compact_vip_and_storage(status, problems)
        self.assertIn('  vip: gateway VIP has no master', problems)
        self.assertIn('  storage: no storage leader detected', problems)

    def test_compact_storage_services(self):
        status = {
            'vipStatus': {},
            'leadership': {'storage_leader': 's1'},
            'hostHealth': {'s1': {'services': {
                'ceph': {'status': 'healthy'},
                'postgresql': {'status': 'stopped', 'details': 'down'},
            }}},
        }
        problems = []
        text = render(views._print_compact_vip_and_storage, status, problems)
        self.assertIn('leader s1, pg stopped, qdrant unknown, registry unknown', text)
        self.assertIn('  storage: postgresql stopped on s1 - down', problems)

    def test_compact_dhcp_summary_cases(self):
        problems = []
        views._print_compact_dhcp([], problems)
        views._print_compact_dhcp([{'host': 'ip-a', '_node': {'hostname': 's1'}}], problems)
        views._print_compact_dhcp([{'host': 'ip-a'}, {'host': 'ip-b'}], problems)
        self.assertIn('no servers running', render(views._print_compact_dhcp, [], []))

    def test_compact_dns_filters_non_core_and_warns(self):
        problems = []
        results = [
            {'_node': {'hostname': 's1'}, 'running': True, 'dns_working': False, 'dns_error': 'missing'},
            {'_node': {'hostname': 'amt'}, 'running': True, 'dns_working': True},
            {'_node': {'hostname': 's2'}, 'running': False},
            {'_node': {'hostname': 's3'}, 'running': True, 'dns_working': True},
        ]
        text = render(views._print_compact_dns, results, problems)
        self.assertIn('1/3 core nodes ok', text)
        self.assertEqual(problems, ['  dns s1: missing', '  dns s2: not running'])

    def test_compact_problems(self):
        self.assertIn('No problems', render(views._print_compact_problems, []))
        self.assertIn('  bad', render(views._print_compact_problems, ['  bad']))

    def test_verbose_etcd(self):
        text = render(views._print_verbose_etcd, [
            {'host': 'b', 'healthy': True, 'is_leader': True, 'version': '3', 'db_size': 10},
            {'host': 'a', 'healthy': False, 'error': 'down'},
        ])
        self.assertIn('a: Unhealthy', text)
        self.assertIn('b: Healthy (LEADER)', text)

    def test_verbose_dhcp(self):
        running = [{'host': 'ip', '_node': {'hostname': 's1'}, 'etcd_connected': True, 'server_ip': 'ip'}]
        down = [{'host': 'down', '_node': {'hostname': 's2'}, 'error': 'no', 'running': False}]
        results = running + down
        self.assertIn('DHCP running', render(views._print_verbose_dhcp, running))
        self.assertIn('No DHCP', render(views._print_verbose_dhcp_summary, down, []))
        self.assertIn('single server', render(views._print_verbose_dhcp_summary, running, running))
        self.assertIn('Multiple DHCP', render(views._print_verbose_dhcp_summary, running * 2, running * 2))

    def test_verbose_dns_results(self):
        good = {'_node': {'hostname': 's1'}, 'running': True, 'dns_working': True,
                'hostname': 's1', 'host': 'ip1', 'resolved_ip': 'ip-a',
                'amt_hostname': 's1a', 'amt_resolved_ip': 'ip-b'}
        warn = {'_node': {'hostname': 's2'}, 'host': 'ip2', 'running': True, 'dns_working': False, 'dns_error': 'bad'}
        down = {'_node': {'hostname': 's3'}, 'host': 'ip3', 'running': False, 'error': 'down'}
        text = render(views._print_verbose_dns, [good, warn, down])
        self.assertIn('DNS OK', text)
        self.assertIn('running but', text)
        self.assertIn('not running', text)

    def test_verbose_vips_and_certificate(self):
        status = {
            'vipStatus': {
                'gateway_vip': {'master_hostname': 's1', 'ip': 'ip-a', 'interface': 'eth0'},
                'storage_vip': {'master_hostname': '', 'ip': 'ip-b', 'interface': 'eth0'},
                'keepalived_nodes': [
                    {'hostname': 's1', 'keepalived_active': True},
                    {'hostname': 's2', 'keepalived_active': False},
                ],
            },
            'leadership': {'storage_leader': 's1', 'dhcp_leader': 's2'},
            'hostHealth': {'s1': {'services': {'ceph': {'status': 'healthy'}, 'postgresql': {'status': 'degraded'}}}},
            'certificateStatus': {'status': 'healthy', 'details': {'days_until_expiry': 10, 'subject': 'cert'}},
        }
        text = render(views._print_verbose_storage, status)
        self.assertIn('active on s1', text)
        self.assertIn('no master', text)
        self.assertIn('Keepalived', text)
        self.assertIn('TLS certificate', text)

    def test_verbose_storage_unreachable(self):
        self.assertIn('unreachable', render(views._print_verbose_storage, {'_error': 'down'}))

    def test_verbose_nodes_branches(self):
        self.assertIn('Could not connect', render(views._print_verbose_nodes, None, []))
        self.assertIn('No nodes registered', render(views._print_verbose_nodes, object(), []))
        node = {'hostname': 's1', 'type': 'core', 'ip': 'ip-a', 'mac': 'aa',
                'allocated_at': '2026-08-30T10:00:00Z'}
        self.assertIn('2026-08-30 10:00', render(views._print_verbose_nodes, object(), [node]))

    def test_run_compact_renders_all_subsystems(self):
        marks = []
        timer = lambda label: marks.append(label)
        etcd_results = [
            {'healthy': True, 'host': 'a', 'is_leader': True},
            {'healthy': True, 'host': 'b', 'is_leader': False},
        ]
        status = {'vipStatus': {}, 'leadership': {}, 'hostHealth': {}}
        nodes = [{'hostname': 's1', 'ip': 'ip-a'}]
        with mock.patch.object(views, 'get_cluster_members', return_value=['a']), \
             mock.patch.object(views, 'check_etcd_host', side_effect=etcd_results), \
             mock.patch.object(views, 'get_etcd_client', return_value=object()), \
             mock.patch.object(views, 'get_all_nodes', return_value=nodes), \
             mock.patch.object(views, 'fetch_cluster_status', return_value=status), \
             mock.patch.object(views, 'check_dhcp_service_on_host', return_value={'running': True, 'host': 'ip-a'}), \
             mock.patch.object(views, 'check_dnsmasq_service_on_host', return_value={'running': True, 'dns_working': True}):
            text = render(views._run_compact, timer)
        self.assertIn('Cluster Health', text)
        self.assertIn('Problems:', text)
        self.assertIn('dhcp_dns_per_node', marks)

    def test_run_verbose_without_client(self):
        marks = []
        timer = lambda label: marks.append(label)
        with mock.patch.object(views, 'get_cluster_members', return_value=['a']), \
             mock.patch.object(views, 'check_etcd_host', return_value={'healthy': True, 'host': 'a', 'is_leader': True, 'version': '3', 'db_size': 1}), \
             mock.patch.object(views, 'get_etcd_client', return_value=None), \
             mock.patch.object(views, 'fetch_cluster_status', return_value={'_error': 'down'}):
            text = render(views._run_verbose, timer)
        self.assertIn('ETCD Cluster Health Check', text)
        self.assertIn('Could not connect', text)
        self.assertNotIn('dhcp_dns_per_node', marks)


if __name__ == '__main__':
    unittest.main()
