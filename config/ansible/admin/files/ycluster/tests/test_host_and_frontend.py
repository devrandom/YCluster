"""Tests for etcd-backed host state and frontend node management."""

import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from ycluster.utils import frontend_manager

TEST_IP = ".".join(str(x) for x in (203, 0, 113, 2))
from ycluster.utils import host_state


class FakeClient:
    def __init__(self, values=None, prefix=None):
        self.values = values or {}
        self.prefix = prefix or []
        self.puts = []
        self.deletes = []

    def get(self, key):
        value = self.values.get(key)
        if value is None:
            return None, None
        return value, None

    def put(self, key, value):
        self.puts.append((key, value))

    def delete(self, key):
        self.deletes.append(key)

    def get_prefix(self, prefix):
        return self.prefix


class HostStateTests(unittest.TestCase):
    def setUp(self):
        self.allocation = json.dumps(
            {'hostname': 'host-a', 'mac': 'mac-a'}).encode()
        self.client = FakeClient({
            '/cluster/nodes/by-hostname/host-a': self.allocation})

    def test_get_allocation_requires_record(self):
        with self.assertRaises(KeyError):
            host_state._get_allocation(FakeClient(), 'missing')
        self.assertEqual(
            host_state._get_allocation(self.client, 'host-a'),
            {'hostname': 'host-a', 'mac': 'mac-a'})

    def test_set_host_disabled_writes_both_keys(self):
        with mock.patch.object(host_state, 'get_etcd_client',
                               return_value=self.client):
            allocation = host_state.set_host_disabled('host-a', True)
        self.assertTrue(allocation['disabled'])
        self.assertEqual(self.client.puts, [
            ('/cluster/nodes/by-hostname/host-a', json.dumps(allocation)),
            ('/cluster/nodes/by-mac/mac-a', json.dumps(allocation)),
        ])

    def test_set_drain_adds_and_removes_flag(self):
        with mock.patch.object(host_state, 'get_etcd_client',
                               return_value=self.client):
            host_state.set_drain('host-a', True)
        self.assertEqual(self.client.puts[-1],
                         ('/cluster/nodes/host-a/drain', 'true'))
        with mock.patch.object(host_state, 'get_etcd_client',
                               return_value=self.client):
            host_state.set_drain('host-a', False)
        self.assertEqual(self.client.deletes,
                         ['/cluster/nodes/host-a/drain'])

        missing = FakeClient()
        with mock.patch.object(host_state, 'get_etcd_client',
                               return_value=missing):
            with self.assertRaises(KeyError):
                host_state.set_drain('missing', True)


class FrontendManagerTests(unittest.TestCase):
    def test_get_frontend_nodes_skips_malformed_and_empty(self):
        client = FakeClient(prefix=[
            (b'{"name":"b"}', None),
            (b'not-json', None),
            (b'', None),
        ])
        self.assertEqual(frontend_manager.get_frontend_nodes(client),
                         [{'name': 'b'}])

    def test_list_frontend_nodes_empty_and_sorted(self):
        client = FakeClient(prefix=[
            (json.dumps({'name': 'b', 'hostname': 'host-b',
                         'added': 'when-b'}).encode(), None),
            (json.dumps({'name': 'a', 'ip': TEST_IP}).encode(), None),
        ])
        output = io.StringIO()
        with mock.patch.object(frontend_manager, 'get_frontend_nodes',
                               return_value=[]), \
                contextlib.redirect_stdout(output):
            frontend_manager.list_frontend_nodes()
        self.assertIn('No frontend nodes found', output.getvalue())

        with mock.patch.object(frontend_manager, 'get_frontend_nodes',
                               return_value=frontend_manager
                               .get_frontend_nodes(client)), \
                contextlib.redirect_stdout(output):
            frontend_manager.list_frontend_nodes()
        self.assertLess(output.getvalue().index('a'),
                        output.getvalue().index('b'))

    def test_add_frontend_node_rejects_existing(self):
        client = FakeClient({'/cluster/nodes/frontend/a': b'existing'})
        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=client):
            self.assertFalse(frontend_manager.add_frontend_node('a', 'x'))

    def test_add_frontend_node_ip_hostname_and_description(self):
        client = FakeClient()
        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=client), \
                mock.patch('builtins.print') as print_:
            self.assertTrue(frontend_manager.add_frontend_node(
                'a', TEST_IP, 'edge'))
            self.assertTrue(frontend_manager.add_frontend_node('b', 'host-b'))
        self.assertEqual(len(client.puts), 2)
        self.assertEqual(json.loads(client.puts[0][1])['ip'], TEST_IP)
        self.assertEqual(json.loads(client.puts[1][1])['hostname'], 'host-b')
        self.assertEqual(len(print_.mock_calls), 2)

    def test_delete_frontend_node(self):
        client = FakeClient()
        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=client), \
                mock.patch('builtins.print') as print_:
            self.assertFalse(frontend_manager.delete_frontend_node('a'))
            client.values['/cluster/nodes/frontend/a'] = b'x'
            self.assertTrue(frontend_manager.delete_frontend_node('a'))
        self.assertEqual(client.deletes, ['/cluster/nodes/frontend/a'])
        self.assertEqual(len(print_.mock_calls), 2)

    def test_show_frontend_node(self):
        node = json.dumps({'name': 'a', 'ip': TEST_IP}).encode()
        client = FakeClient({'/cluster/nodes/frontend/a': node})
        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=client), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(frontend_manager.show_frontend_node('a'))
        self.assertIn('Frontend Node: a', output.getvalue())

        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=FakeClient()):
            self.assertFalse(frontend_manager.show_frontend_node('missing'))

        client.values['/cluster/nodes/frontend/b'] = b'bad'
        with mock.patch.object(frontend_manager, 'get_etcd_client',
                               return_value=client):
            self.assertFalse(frontend_manager.show_frontend_node('b'))

    def test_main_dispatches_commands(self):
        with mock.patch.object(frontend_manager, 'list_frontend_nodes') as cmd:
            with mock.patch.object(sys, 'argv',
                                   ['frontend', 'list']):
                frontend_manager.main()
            cmd.assert_called_once_with()
        with mock.patch.object(frontend_manager, 'add_frontend_node') as cmd:
            with mock.patch.object(sys, 'argv', [
                'frontend', 'add', 'a', TEST_IP, '--description',
                'edge']):
                frontend_manager.main()
            cmd.assert_called_once_with('a', TEST_IP, 'edge')
        with mock.patch.object(frontend_manager, 'delete_frontend_node') as cmd:
            with mock.patch.object(sys, 'argv',
                                   ['frontend', 'delete', 'a']):
                frontend_manager.main()
            cmd.assert_called_once_with('a')
        with mock.patch.object(frontend_manager, 'show_frontend_node') as cmd:
            with mock.patch.object(sys, 'argv',
                                   ['frontend', 'show', 'a']):
                frontend_manager.main()
            cmd.assert_called_once_with('a')

    def test_main_without_command_prints_help(self):
        output = io.StringIO()
        with mock.patch('sys.stdout', output), \
                mock.patch.object(sys, 'argv', ['frontend']):
            frontend_manager.main()
        self.assertIn('usage:', output.getvalue())


if __name__ == '__main__':
    unittest.main()
