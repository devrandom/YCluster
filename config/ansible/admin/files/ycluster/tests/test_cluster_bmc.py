import unittest
from unittest import mock

from ycluster.cli import cluster


class ClusterBmcGetTests(unittest.TestCase):
    def test_prints_all_recorded_macs(self):
        client = mock.Mock()
        client.get_prefix.return_value = [
            (b'{"bmc_mac": "aa"}', type('Meta', (), {'key': b'/cluster/nodes/hardware/a'})),
            (b'{}', type('Meta', (), {'key': b'/cluster/nodes/hardware/b'})),
        ]
        with mock.patch('builtins.print') as printed:
            cluster._print_all_bmc_macs(client)
        printed.assert_called_once_with('a: aa')

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


if __name__ == '__main__':
    unittest.main()
