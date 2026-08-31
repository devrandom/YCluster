"""Tests for DHCP allocation and lease persistence helpers."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from ycluster.utils import dhcp_allocation
from ycluster.utils import dhcp_leases


def ip(last):
    return f"10.0.0.{last}"


def amt_ip(last):
    return f"10.10.10.{last}"


class FakeComparator:
    def __init__(self, op, key):
        self.op = op
        self.key = key

    def __eq__(self, other):
        return (self.op, self.key, other)


class FakeClient:
    def __init__(self, get_results=None, prefix_results=None):
        self.get_results = get_results or {}
        self.prefix_results = prefix_results or {}
        self.puts = []
        self.deletes = []
        self.transaction_args = None
        self.transaction_result = (True, None)
        self.transactions = SimpleNamespace(
            mod=lambda key: FakeComparator('mod', key),
            version=lambda key: FakeComparator('version', key),
            put=lambda key, value: ('put', key, value),
            delete=lambda key: ('delete', key),
        )

    def get(self, key):
        return self.get_results.get(key, (None, None))

    def get_prefix(self, prefix):
        return self.prefix_results.get(prefix, [])

    def put(self, key, value):
        self.puts.append((key, value))

    def delete(self, key):
        self.deletes.append(key)

    def transaction(self, **kwargs):
        self.transaction_args = kwargs
        return self.transaction_result


class FakeServer(dhcp_allocation.DHCPAllocationMixin,
                 dhcp_leases.DHCPLeaseStore):
    def __init__(self, client=None):
        self.client = client
        self.leases = {}
        self.allocated_ips = set()

    def get_etcd_client(self):
        return self.client


class AllocationTests(unittest.TestCase):
    def test_get_server_ip_from_netplan(self):
        server = FakeServer()
        result = SimpleNamespace(stdout=f'addresses:\n  - {ip(7)}/24\n')
        with mock.patch.object(dhcp_allocation.subprocess, 'run',
                               return_value=result):
            self.assertEqual(server.get_server_ip(), ip(7))

    def test_get_server_ip_fallback_and_failure(self):
        server = FakeServer()
        with mock.patch.object(dhcp_allocation.subprocess, 'run',
                               side_effect=dhcp_allocation.subprocess
                               .CalledProcessError(1, 'netplan')), \
                mock.patch.object(dhcp_allocation.socket, 'gethostname',
                                  return_value='s2'), \
                mock.patch.object(server, 'determine_ip_from_hostname',
                                  return_value=ip(12)):
            self.assertEqual(server.get_server_ip(), ip(12))

        with mock.patch.object(dhcp_allocation.subprocess, 'run',
                               side_effect=KeyError('netplan')), \
                mock.patch.object(dhcp_allocation.socket, 'gethostname',
                                  return_value='unknown'), \
                mock.patch.object(server, 'determine_ip_from_hostname',
                                  return_value=None):
            with self.assertRaises(ValueError):
                server.get_server_ip()

    def test_determine_ip_from_hostname(self):
        server = FakeServer()
        self.assertIsNone(server.determine_ip_from_hostname(''))
        self.assertIsNone(server.determine_ip_from_hostname('unknown1'))
        self.assertIsNone(server.determine_ip_from_hostname('sa'))
        self.assertIsNone(server.determine_ip_from_hostname('s21'))
        self.assertEqual(server.determine_ip_from_hostname('s2'), ip(12))
        self.assertEqual(server.determine_ip_from_hostname('nas3a'),
                         amt_ip(133))
        self.assertEqual(server.determine_ip_from_hostname('nas3'),
                         ip(133))

    def test_machine_type_detection(self):
        server = FakeServer()
        self.assertEqual(server.determine_type_from_mac(''), 'compute')
        self.assertEqual(server.determine_type_from_mac('58:47:CA:1:2:3'),
                         'storage')
        self.assertEqual(server.determine_type_from_mac('aa-bb'), 'compute')
        self.assertEqual(server.determine_type_from_hostname(''), 'compute')
        self.assertEqual(server.determine_type_from_hostname('nas4'), 'nas')
        self.assertEqual(server.determine_type_from_hostname('nv2'), 'nvidia')
        self.assertEqual(server.determine_type_from_hostname('s2'), 'storage')
        self.assertEqual(server.determine_type_from_hostname('c1'), 'compute')
        self.assertEqual(server.determine_type_from_hostname('m1'), 'macos')
        self.assertEqual(server.determine_type_from_hostname('x1'), 'adhoc')
        self.assertEqual(server.determine_type_from_hostname('zz4'), 'compute')

    def test_get_next_dynamic_ip_skips_bad_and_allocated_entries(self):
        entries = [
            (json.dumps({'ip': ip(200)}).encode(), SimpleNamespace()),
            (b'not-json', SimpleNamespace()),
            (b'', SimpleNamespace()),
        ]
        server = FakeServer(FakeClient(
            prefix_results={'/cluster/nodes/by-hostname/': entries}))
        server.leases = {'mac': {'ip': '10.0.0.bad'}, 'mac2': {
            'ip': ip(201)}}
        self.assertEqual(server.get_next_dynamic_ip(), ip(202))

    def test_get_next_dynamic_ip_full(self):
        entries = [(json.dumps({'ip': f'10.0.0.{n}'}).encode(),
                    SimpleNamespace()) for n in range(200, 250)]
        server = FakeServer(FakeClient(
            prefix_results={'/cluster/nodes/by-hostname/': entries}))
        self.assertIsNone(server.get_next_dynamic_ip())

    def test_get_next_hostname(self):
        server = FakeServer(FakeClient(prefix_results={
            '/cluster/nodes/by-hostname/s': [
                (json.dumps({'hostname': 's1'}).encode(),
                 SimpleNamespace(key=b'/cluster/nodes/by-hostname/s1')),
                (json.dumps({'hostname': 's3'}).encode(),
                 SimpleNamespace(key=b'/cluster/nodes/by-hostname/s3')),
                (b'', SimpleNamespace(key=b'ignored')),
                (json.dumps({}).encode(),
                 SimpleNamespace(key=b'/cluster/nodes/by-hostname/not-a-num')),
            ]}))
        self.assertEqual(server.get_next_hostname('storage'), 's2')
        server.client = FakeClient(prefix_results={
            '/cluster/nodes/by-hostname/c': [
                (json.dumps({'hostname': 'c1'}).encode(),
                 SimpleNamespace(key=b'/cluster/nodes/by-hostname/c1'))]})
        self.assertEqual(server.get_next_hostname('compute'), 'c2')
        server.client = None
        self.assertEqual(server.get_next_hostname('unknown'), 'c1')

    def test_allocate_hostname_and_ip_retries_limit(self):
        server = FakeServer()
        with mock.patch.object(server, '_allocate_attempt',
                               side_effect=[dhcp_allocation._ALLOC_RETRY] * 3):
            self.assertEqual(server.allocate_hostname_and_ip('MAC'),
                             (None, None))

        with mock.patch.object(
                server, '_allocate_attempt',
                side_effect=[dhcp_allocation._ALLOC_RETRY, ('c1', ip(1))]):
            self.assertEqual(server.allocate_hostname_and_ip('MAC'),
                             ('c1', ip(1)))

    def test_allocate_attempt_requires_client(self):
        server = FakeServer(None)
        self.assertEqual(server._allocate_attempt('MAC'), (None, None))

    def test_allocate_attempt_ignores_amt_hostname(self):
        server = FakeServer()
        server.client = FakeClient()
        with mock.patch.object(server, '_allocate_attempt',
                               return_value=('old', ip(1))):
            self.assertEqual(
                server.allocate_hostname_and_ip('MAC', 's2a'),
                ('old', ip(1)))
        self.assertEqual(server._allocate_attempt('MAC', 's2a'),
                         ('dhcp-200', ip(200)))

    def test_allocate_attempt_returns_existing(self):
        value = json.dumps({'hostname': 'c1', 'ip': ip(51)}).encode()
        client = FakeClient(get_results={
            '/cluster/nodes/by-mac/mac': (
                value, SimpleNamespace(mod_revision=7))})
        server = FakeServer(client)
        self.assertEqual(server._allocate_attempt('MAC'), ('c1', ip(51)))
        self.assertEqual(
            server._allocate_attempt('MAC', 'c1'), ('c1', ip(51)))

    def test_allocate_attempt_commits_new_allocation(self):
        client = FakeClient()
        server = FakeServer(client)
        with mock.patch.object(server, 'get_next_dynamic_ip',
                               return_value=ip(200)):
            self.assertEqual(server._allocate_attempt('MAC'),
                             ('dhcp-200', ip(200)))
        self.assertEqual(client.transaction_args['compare'], [
            ('version', '/cluster/nodes/by-mac/mac', 0),
            ('version', '/cluster/nodes/by-hostname/dhcp-200', 0),
        ])
        self.assertEqual(client.transaction_args['success'], [
            ('put', '/cluster/nodes/by-mac/mac',
             client.transaction_args['success'][0][2]),
            ('put', '/cluster/nodes/by-hostname/dhcp-200',
             client.transaction_args['success'][0][2]),
        ])

    def test_allocate_attempt_uses_available_hostname(self):
        client = FakeClient()
        server = FakeServer(client)
        self.assertEqual(server._allocate_attempt('MAC', 's2'),
                         ('s2', ip(12)))
        self.assertEqual(client.transaction_args['compare'], [
            ('version', '/cluster/nodes/by-mac/mac', 0),
            ('version', '/cluster/nodes/by-hostname/s2', 0),
        ])

    def test_allocate_attempt_lost_race_returns_retry(self):
        value = json.dumps({'hostname': 'old', 'ip': ip(12),
                            'mac': 'mac'}).encode()
        client = FakeClient(get_results={
            '/cluster/nodes/by-mac/mac': (
                value, SimpleNamespace(mod_revision=7)),
            '/cluster/nodes/by-hostname/s2': (
                json.dumps({'mac': 'mac'}).encode(),
                SimpleNamespace(mod_revision=9)),
        })
        client.transaction_result = (False, None)
        server = FakeServer(client)
        self.assertIs(server._allocate_attempt('mac', 's2'),
                      dhcp_allocation._ALLOC_RETRY)
        self.assertEqual(client.transaction_args['compare'][1],
                         ('mod', '/cluster/nodes/by-hostname/s2', 9))
        self.assertEqual(client.transaction_args['success'][-1],
                         ('delete', '/cluster/nodes/by-hostname/old'))

    def test_allocate_attempt_without_dynamic_ip(self):
        server = FakeServer(FakeClient())
        with mock.patch.object(server, 'get_next_dynamic_ip',
                               return_value=None):
            self.assertEqual(server._allocate_attempt('MAC'), (None, None))

    def test_allocate_attempt_transaction_failure(self):
        client = FakeClient()
        client.transaction = mock.Mock(side_effect=RuntimeError('boom'))
        server = FakeServer(client)
        with mock.patch.object(server, 'get_next_dynamic_ip',
                               return_value=ip(200)):
            self.assertEqual(server._allocate_attempt('MAC'), (None, None))

    def test_allocate_attempt_taken_and_invalid_hostnames(self):
        value = json.dumps({'hostname': 'other', 'ip': ip(51),
                            'mac': 'other'}).encode()
        client = FakeClient(get_results={
            '/cluster/nodes/by-hostname/s3': (
                value, SimpleNamespace(mod_revision=3))})
        server = FakeServer(client)
        with mock.patch.object(server, 'get_next_dynamic_ip',
                               return_value=ip(200)) as dynamic:
            self.assertEqual(server._allocate_attempt('MAC', 's3'),
                             ('dhcp-200', ip(200)))
            self.assertEqual(server._allocate_attempt('MAC', 'bad!'),
                             ('dhcp-200', ip(200)))
            self.assertEqual(dynamic.call_count, 2)


class LeaseTests(unittest.TestCase):
    def test_migrate_without_client(self):
        server = FakeServer(None)
        self.assertIsNone(server.migrate_leases_to_normalized_mac())

    def test_migrate_leases(self):
        changed = json.dumps({'mac': 'AA:BB'}).encode()
        invalid = b'not-json'
        no_mac = json.dumps({}).encode()
        same_key = json.dumps({'mac': 'cc'}).encode()
        bad_value = json.dumps({'mac': 'dd'}).encode()
        client = FakeClient(prefix_results={
            '/cluster/dhcp/leases/': [
                (changed, SimpleNamespace(key=b'/leases/old')),
                (invalid, SimpleNamespace(key=b'/leases/invalid')),
                (no_mac, SimpleNamespace(key=b'/leases/no-mac')),
                (same_key, SimpleNamespace(key=b'/leases/cc')),
                (bad_value, SimpleNamespace(key=b'/leases/bad')),
                (b'', SimpleNamespace(key=b'/leases/empty')),
            ]})
        client.delete = mock.Mock()
        client.delete.side_effect = [None, RuntimeError('delete boom')]
        server = FakeServer(client)
        server.migrate_leases_to_normalized_mac()
        self.assertEqual(client.puts, [
            ('/cluster/dhcp/leases/aabb', json.dumps({'mac': 'AA:BB'})),
            ('/cluster/dhcp/leases/dd', json.dumps({'mac': 'dd'})),
        ])

    def test_migrate_outer_error(self):
        client = FakeClient()
        client.get_prefix = mock.Mock(side_effect=RuntimeError('outer'))
        server = FakeServer(client)
        server.migrate_leases_to_normalized_mac()

    def test_migrate_no_leases(self):
        client = FakeClient()
        server = FakeServer(client)
        server.migrate_leases_to_normalized_mac()
        self.assertEqual(client.puts, [])

    def lease_server(self):
        client = FakeClient()
        server = FakeServer(client)
        return client, server

    def test_load_leases_without_client(self):
        _, server = self.lease_server()
        server.client = None
        self.assertIsNone(server.load_leases_from_etcd())

    def test_load_leases_adds_legacy_fields(self):
        client, server = self.lease_server()
        client.prefix_results['/cluster/dhcp/leases/'] = [
            (json.dumps({'mac': 'aa', 'ip': ip(200)}).encode(),
             SimpleNamespace(key=b'/leases/aa')),
            (b'not-json', SimpleNamespace(key=b'/leases/bad')),
            (json.dumps({'no-mac': True}).encode(),
             SimpleNamespace(key=b'/leases/bad2')),
        ]
        server.load_leases_from_etcd()
        self.assertIn('aa', server.leases)
        self.assertEqual(server.allocated_ips, {ip(200)})

    def test_load_leases_outer_error(self):
        client, server = self.lease_server()
        client.get_prefix = mock.Mock(side_effect=RuntimeError('outer'))
        server.load_leases_from_etcd()
        self.assertEqual(server.leases, {})

    def test_save_lease_to_etcd(self):
        client, server = self.lease_server()
        server.client = None
        self.assertFalse(server.save_lease_to_etcd('aa', {'ip': 'ip'}))
        server.client = client
        self.assertTrue(server.save_lease_to_etcd(
            'AA', {'ip': ip(200)}))
        client.put = mock.Mock(side_effect=RuntimeError('boom'))
        self.assertFalse(server.save_lease_to_etcd('AA', {'ip': 'ip'}))

    def test_get_or_create_existing_and_temporary(self):
        _, server = self.lease_server()
        server.leases = {'aa': {
            'mac': 'aa', 'ip': ip(200), 'hostname': 'host',
            'expires': '9999-01-01T00:00:00'}}
        server.allocated_ips = {ip(200)}
        self.assertEqual(server.get_or_create_lease('aa'),
                         (ip(200), 'host'))
        self.assertEqual(server.get_or_create_lease('aa', 'ignored', ''),
                         (ip(200), 'host'))
        self.assertEqual(
            server.get_or_create_lease('aa', 'ignored', 'ubuntu-server'),
            (ip(200), 'host'))

    def test_get_or_create_expired_or_changed_lease(self):
        client, server = self.lease_server()
        server.leases = {'aa': {
            'mac': 'aa', 'ip': ip(199), 'hostname': 'old',
            'expires': '9999-01-01T00:00:00'}}
        server.allocated_ips = {ip(199)}
        with mock.patch.object(server, 'allocate_hostname_and_ip',
                               return_value=('new', ip(200))) as allocate:
            self.assertEqual(
                server.get_or_create_lease('aa', None, 'new'),
                (ip(200), 'new'))
        self.assertEqual(allocate.call_args, mock.call('aa', 'new'))
        self.assertNotIn(ip(199), server.allocated_ips)
        self.assertNotIn('old', server.leases)

        server.leases = {'bb': {
            'mac': 'bb', 'ip': ip(198), 'hostname': 'old',
            'expires': '2000-01-01T00:00:00'}}
        server.allocated_ips = {ip(198)}
        with mock.patch.object(server, 'allocate_hostname_and_ip',
                               return_value=(None, None)):
            self.assertEqual(server.get_or_create_lease('bb'), (None, None))

    def test_get_or_create_new_lease(self):
        client, server = self.lease_server()
        with mock.patch.object(dhcp_leases, 'LEASE_TIME', 60), \
                mock.patch.object(server, 'allocate_hostname_and_ip',
                                  return_value=('new', ip(200))):
            self.assertEqual(server.get_or_create_lease('aa'),
                             (ip(200), 'new'))
        self.assertEqual(server.allocated_ips, {ip(200)})
        self.assertEqual(len(client.puts), 1)


if __name__ == '__main__':
    unittest.main()
