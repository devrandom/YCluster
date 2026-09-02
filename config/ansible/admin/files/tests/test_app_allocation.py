"""Unit tests for the hostname and IP allocation helpers."""

import json
import unittest
from unittest import mock

import app_allocation


def allocation_json(**overrides):
    data = {
        'hostname': 's2',
        'type': 'storage',
        'ip': '10.0.0.12',
        'amt_ip': '10.10.10.12',
        'via_wg': False,
        'updated_at': 'old',
    }
    data.update(overrides)
    return json.dumps(data)


class AllocationHelpersTests(unittest.TestCase):
    def test_host_location_parsing(self):
        self.assertIsNone(app_allocation._parse_host_location('unknown1'))
        self.assertIsNone(app_allocation._parse_host_location('sbad'))
        self.assertEqual(app_allocation._parse_host_location('nas3'),
                         ('nas', 3, False))
        self.assertEqual(app_allocation._parse_host_location('nv3a'),
                         ('nv', 3, True))

    def test_host_ip_validation(self):
        with self.assertRaises(ValueError):
            app_allocation._host_ip('nas', 11, False, False)

    def test_mac_type_detection(self):
        self.assertEqual(app_allocation.determine_type_from_mac(''), 'compute')
        self.assertEqual(app_allocation.determine_type_from_mac('58:47:CA:00:00:01'),
                         'storage')
        self.assertEqual(app_allocation.determine_type_from_mac('5847ca00'), 'storage')
        self.assertEqual(app_allocation.determine_type_from_mac('aa-bb-cc'), 'compute')

    def test_get_next_hostname_finds_gap_and_max(self):
        client = mock.Mock()
        client.get_prefix.return_value = []
        self.assertEqual(app_allocation.get_next_hostname(client, 'storage'), 's1')

        meta = mock.Mock()
        meta.key = b'/cluster/nodes/by-hostname/nv3'
        client.get_prefix.return_value = [(b'{}', meta), (None, meta), (b'bad', meta)]
        self.assertEqual(app_allocation.get_next_hostname(client, 'nvidia'), 'nv1')

        meta.key = b'/cluster/nodes/by-hostname/nv1'
        client.get_prefix.return_value = [(b'{}', meta), (None, meta)]
        self.assertEqual(app_allocation.get_next_hostname(client, 'nvidia'), 'nv2')

        meta.key = b'/cluster/nodes/by-hostname/nv1'
        client.get_prefix.return_value = [(b'{}', meta)]
        self.assertEqual(app_allocation.get_next_hostname(client, 'unknown'), 'c1')


class GetOrCreateAllocationTests(unittest.TestCase):
    def test_existing_allocation_recomputes_transport_and_type(self):
        client = mock.Mock()
        meta = mock.Mock()
        client.get.return_value = (allocation_json().encode(), meta)
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client), \
                mock.patch.object(app_allocation, 'get_next_hostname',
                                  return_value='c1'), \
                mock.patch.object(app_allocation, 'determine_ip_from_hostname',
                                  side_effect=lambda host, via_wg=False:
                                  'ip-amt' if host == 'c1a' else 'ip-wg'), \
                mock.patch.object(app_allocation, 'print'):
            got, created = app_allocation.get_or_create_allocation(
                'AA:BB', node_type='compute', via_wg=True)

        self.assertEqual(got['hostname'], 'c1')
        self.assertEqual(got['ip'], 'ip-wg')
        self.assertEqual(got['amt_ip'], 'ip-amt')
        self.assertEqual(got['type'], 'compute')
        client.delete.assert_called_once_with('/cluster/nodes/by-hostname/s2')
        self.assertEqual(client.put.call_count, 4)

    def test_existing_allocation_without_changes(self):
        client = mock.Mock()
        client.get.return_value = (allocation_json().encode(), mock.Mock())
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client):
            got, created = app_allocation.get_or_create_allocation('aabb')
        self.assertEqual(got['hostname'], 's2')
        self.assertFalse(created)
        client.put.assert_not_called()

    def test_existing_allocation_fills_legacy_amt_ip(self):
        client = mock.Mock()
        data = json.loads(allocation_json())
        del data['amt_ip']
        client.get.return_value = (json.dumps(data).encode(), mock.Mock())
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client), \
                mock.patch.object(app_allocation, 'determine_ip_from_hostname',
                                  return_value='ip-legacy-amt'):
            got, created = app_allocation.get_or_create_allocation('aabb')
        self.assertEqual(got['amt_ip'], 'ip-legacy-amt')

    def test_new_allocation_commits_transaction(self):
        client = mock.Mock()
        client.get.return_value = (None, None)
        client.transaction.return_value = (True, None)
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client), \
                mock.patch.object(app_allocation, 'get_next_hostname',
                                  return_value='c1'):
            got, created = app_allocation.get_or_create_allocation(
                'aa:bb', node_type='compute', via_wg=True)

        self.assertEqual(got['hostname'], 'c1')
        self.assertTrue(created)
        self.assertEqual(got['via_wg'], True)
        self.assertEqual(client.transactions.put.call_count, 2)

    def test_lost_race_returns_winner(self):
        client = mock.Mock()
        meta = mock.Mock()
        client.get.side_effect = [
            (None, None),
            (allocation_json(hostname='c3').encode(), meta),
        ]
        client.transaction.return_value = (False, None)
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client), \
                mock.patch.object(app_allocation, 'get_next_hostname',
                                  return_value='c1'):
            got, created = app_allocation.get_or_create_allocation('aabb')
        self.assertEqual(got['hostname'], 'c3')

    def test_double_check_returns_locked_allocation(self):
        client = mock.Mock()
        meta = mock.Mock()
        client.get.side_effect = [(None, None),
                                   (allocation_json().encode(), meta)]
        with mock.patch.object(app_allocation, 'get_etcd_client',
                               return_value=client):
            got, created = app_allocation.get_or_create_allocation('aabb')
        self.assertEqual(got['hostname'], 's2')


if __name__ == '__main__':
    unittest.main()
