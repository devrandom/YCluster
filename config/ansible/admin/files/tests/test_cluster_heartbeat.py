"""Tests for cluster heartbeat formatting."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_FILES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_FILES / 'ycluster'))

_SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'cluster-heartbeat.py'
_spec = importlib.util.spec_from_file_location('cluster_heartbeat', _SCRIPT)
cluster_heartbeat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_heartbeat)


class HeartbeatMessageTests(unittest.TestCase):
    def health_data(self):
        return {
            'timestamp': 'timestamp-value',
            'hostname': 'reporter',
            'nodes': {
                'healthy': 1,
                'total': 2,
                'unhealthy_names': ['node-a'],
                'unreachable_names': ['node-b'],
            },
            'unreachable_details_by_node': {'node-b': 'timeout'},
            'unhealthy_services_by_node': {'node-a': ['etcd(unhealthy)']},
            'leadership': {'storage_leader': 'node-a',
                           'dhcp_leader': 'node-b'},
            'vip_status': {
                'gateway_vip': {'master_hostname': 'gateway'},
                'storage_vip': {'master_hostname': 'storage'},
            },
            'certificate': {'days_until_expiry': 30},
            'services': {'etcd': {'status': 'unhealthy'}},
            'local_services': {
                'ceph': {'status': 'degraded'},
                'network': 'not-a-dict',
            },
            'error': 'local error',
            'cluster_error': 'cluster error',
        }

    def test_format_health_message_renders_all_sections(self):
        message = cluster_heartbeat.format_health_message(
            self.health_data(), 'warning')
        self.assertIn('Cluster Status: WARNING', message)
        self.assertIn('Nodes: 1/2 healthy', message)
        self.assertIn('Unhealthy: node-a', message)
        self.assertIn('Unreachable: node-b(timeout)', message)
        self.assertIn('node-a: etcd(unhealthy)', message)
        self.assertIn('Leadership: Storage: node-a DHCP: node-b', message)
        self.assertIn('VIPs: Gateway: gateway  Storage: storage', message)
        self.assertIn('Certificate expires in 30 days', message)
        self.assertIn('Unhealthy Services: etcd', message)
        self.assertIn('Failed: ceph(degraded)', message)
        self.assertIn('Error: local error', message)
        self.assertIn('Cluster Error: cluster error', message)

    def test_node_lines_empty_without_nodes(self):
        self.assertEqual(cluster_heartbeat._node_message_lines({}), [])


class ClusterHealthTests(unittest.TestCase):
    @staticmethod
    def response(payload, status=200):
        return SimpleNamespace(status_code=status, json=lambda: payload)

    def test_cluster_status_merges_nodes_and_metadata(self):
        cluster_status = {
            'hostHealth': {
                'reporter': {'services': {'etcd': {'status': 'healthy'}}},
                'node-a': {'overall': 'unhealthy',
                           'services': {'ceph': {'status': 'degraded'}}},
                'node-b': {'overall': 'timeout',
                           'elapsed_ms': 3,
                           'exception': 'TimeoutError'},
                'node-c': {'overall': 'disabled'},
                'node-d': {'overall': 'unhealthy',
                           'services': {'clock_skew': {'status': 'degraded'}}},
                'node-s1': {'overall': 'unhealthy',
                            'services': {'clock_skew': {'status': 'degraded'}}},
            },
            'leadership': {'storage_leader': 'node-a'},
            'vipStatus': {'gateway_vip': {'master_hostname': 'gateway'}},
            'certificateStatus': {'status': 'warning',
                                  'details': {'days_until_expiry': 9}},
        }
        with mock.patch.object(cluster_heartbeat.requests, 'get',
                               return_value=self.response(cluster_status)):
            health = cluster_heartbeat.get_cluster_health()
        self.assertEqual(health['nodes']['unhealthy'], 2)
        self.assertEqual(health['nodes']['unreachable'], 1)
        self.assertEqual(health['nodes']['healthy'], 2)
        self.assertEqual(health['unhealthy_services_by_node'],
                         {'node-a': ['ceph(degraded)']})
        self.assertEqual(health['unreachable_details_by_node'],
                         {'node-b': '3ms; TimeoutError'})
        self.assertEqual(health['leadership'], cluster_status['leadership'])
        self.assertEqual(health['vip_status'], cluster_status['vipStatus'])
        self.assertEqual(health['certificate']['status'], 'warning')

    def test_cluster_and_local_request_errors_are_recorded(self):
        with mock.patch.object(cluster_heartbeat.requests, 'get',
                               side_effect=RuntimeError('unavailable')):
            health = cluster_heartbeat.get_cluster_health()
        self.assertEqual(health['overall'], 'error')
        self.assertIn('Failed to get local health', health['error'])
        self.assertIn('Failed to get cluster status', health['cluster_error'])

    def test_cluster_status_non_200_is_ignored(self):
        with mock.patch.object(cluster_heartbeat.requests, 'get',
                               return_value=self.response({}, status=404)):
            health = cluster_heartbeat.get_cluster_health()
        self.assertNotIn('cluster_error', health)
        self.assertEqual(health['nodes'], {})

    def test_healthchecks_url_success_and_failure(self):
        client = mock.Mock()
        client.get.return_value = [b'https://checks']
        with mock.patch.object(cluster_heartbeat, 'get_etcd_client',
                               return_value=client):
            self.assertEqual(cluster_heartbeat.get_healthchecks_url(),
                             'https://checks')
        client.get.return_value = []
        with mock.patch.object(cluster_heartbeat, 'get_etcd_client',
                               return_value=client):
            self.assertIsNone(cluster_heartbeat.get_healthchecks_url())
        with mock.patch.object(cluster_heartbeat, 'get_etcd_client',
                               side_effect=RuntimeError('unavailable')):
            self.assertIsNone(cluster_heartbeat.get_healthchecks_url())

    def test_send_heartbeat_without_url(self):
        with mock.patch.object(cluster_heartbeat, 'get_healthchecks_url',
                               return_value=None) as url:
            self.assertFalse(cluster_heartbeat.send_heartbeat())
        url.assert_called_once_with()

    def test_send_heartbeat_post_urls_and_status(self):
        healthy = {'timestamp': 'time', 'hostname': 'reporter'}
        critical = {'timestamp': 'time', 'hostname': 'reporter',
                    'overall': 'unhealthy'}
        self.assertEqual(self._send_with_status(200, healthy), True)
        self.assertEqual(self._send_with_status(500, healthy), False)
        self.assertEqual(self._send_with_status(200, critical), True)

    def test_send_heartbeat_error_notifies_fail_url(self):
        post = mock.Mock(return_value=SimpleNamespace(status_code=200))
        with mock.patch.object(cluster_heartbeat, 'get_healthchecks_url',
                               return_value='https://checks'), \
                mock.patch.object(cluster_heartbeat, 'get_cluster_health',
                                  side_effect=RuntimeError('unavailable')), \
                mock.patch.object(cluster_heartbeat.requests, 'post', post):
            self.assertFalse(cluster_heartbeat.send_heartbeat())
        self.assertEqual(post.call_args.args[0], 'https://checks/fail')

    def _send_with_status(self, status, health_data):
        post = mock.Mock(return_value=SimpleNamespace(status_code=status))
        with mock.patch.object(cluster_heartbeat, 'get_healthchecks_url',
                               return_value='https://checks'), \
                mock.patch.object(cluster_heartbeat, 'get_cluster_health',
                                  return_value=health_data), \
                mock.patch.object(cluster_heartbeat.requests, 'post', post):
            return cluster_heartbeat.send_heartbeat()

    def test_main_exits_on_send_result(self):
        with mock.patch.object(cluster_heartbeat, 'send_heartbeat',
                               return_value=True), \
                self.assertRaises(SystemExit) as success:
            cluster_heartbeat.main()
        self.assertEqual(success.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
