import json
import unittest
from unittest import mock

from ycluster.cli import cluster


class ClusterBmcGetTests(unittest.TestCase):
    def test_prints_all_recorded_macs(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (b'{"bmc_mac": "aa"}', type('Meta', (), {'key': b'/cluster/nodes/hardware/a'})),
            (b'{"bmc_macs": {"dedicated": "cc", "shared": "dd"}}',
             type('Meta', (), {'key': b'/cluster/nodes/hardware/b'})),
            (b'{}', type('Meta', (), {'key': b'/cluster/nodes/hardware/c'})),
        ]
        with mock.patch('builtins.print') as printed:
            cluster._print_all_bmc_macs(client)
        printed.assert_has_calls([mock.call('a: dedicated=aa'),
                                  mock.call('b: dedicated=cc shared=dd')])

    def test_filters_by_role(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (b'{"bmc_macs": {"dedicated": "cc", "shared": "dd"}}',
             type('Meta', (), {'key': b'/cluster/nodes/hardware/b'})),
        ]
        with mock.patch('builtins.print') as printed:
            cluster._print_all_bmc_macs(client, 'shared')
        printed.assert_called_once_with('b: dd')

    def test_host_role_missing(self):
        client = mock.Mock()
        client.get.return_value = (b'{"bmc_mac": "aa"}', None)
        with mock.patch('builtins.print') as printed:
            cluster._print_host_bmc_mac(client, 'h', 'shared')
        printed.assert_called_once_with('No shared BMC MAC recorded for h')

    def test_missing_record_exits(self):
        client = mock.Mock()
        client.get.return_value = (None, None)
        with self.assertRaises(SystemExit):
            cluster._print_host_bmc_mac(client, 'missing')

    def test_record_without_mac(self):
        client = mock.Mock()
        client.get.return_value = (b'{}', None)
        with mock.patch('builtins.print') as printed:
            cluster._print_host_bmc_mac(client, 'host')
        printed.assert_called_once_with('No BMC MAC recorded for host')


class ClusterBmcSetTests(unittest.TestCase):
    def _set(self, record, role, mac):
        client = mock.Mock()
        client.get.return_value = (record, None)
        args = mock.Mock(hostname='h', mac=mac, role=role)
        with mock.patch.object(cluster, 'get_etcd_client', return_value=client), \
             mock.patch('builtins.print'):
            cluster.cluster_bmc_set(args)
        return json.loads(client.put.call_args[0][1])

    def test_shared_role_keeps_legacy_dedicated_mac(self):
        data = self._set(b'{"bmc_mac": "aa"}', 'shared', 'bb')
        self.assertEqual(data['bmc_macs'], {'dedicated': 'aa', 'shared': 'bb'})
        self.assertEqual(data['bmc_mac'], 'aa')

    def test_dedicated_role_updates_legacy_field(self):
        data = self._set(b'{"bmc_macs": {"shared": "bb"}}', 'dedicated', 'cc')
        self.assertEqual(data['bmc_macs'], {'shared': 'bb', 'dedicated': 'cc'})
        self.assertEqual(data['bmc_mac'], 'cc')


if __name__ == '__main__':
    unittest.main()
