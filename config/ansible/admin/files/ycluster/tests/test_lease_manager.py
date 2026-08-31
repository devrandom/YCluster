import unittest
import unittest.mock as mock
import unittest.mock
from datetime import datetime

from ycluster.utils import lease_manager


class LeaseManagerHelpersTests(unittest.TestCase):
    def test_parse_records_sorts_and_reports_errors(self):
        class Client:
            def get_prefix(self, prefix):
                self.prefix = prefix
                return [
                    (b'{"ip": "ip-b"}', type('Meta', (), {'key': b'node-b'})),
                    (b'not-json', type('Meta', (), {'key': b'node-a'})),
                    (b'{"ip": "ip-a"}', type('Meta', (), {'key': b'node-a'})),
                    (None, type('Meta', (), {'key': b'node-c'})),
                ]

        out = []
        with unittest.mock.patch('builtins.print', lambda *args: out.append(args)):
            records = lease_manager._parse_records(Client(), '/prefix/', 'lease')
        self.assertEqual(records, [('node-a', {'ip': 'ip-a'}), ('node-b', {'ip': 'ip-b'})])
        self.assertEqual(len(out), 1)

    def test_format_allocated_at(self):
        self.assertEqual(lease_manager._format_allocated_at('Unknown'), 'Unknown')
        self.assertEqual(lease_manager._format_allocated_at('2025-01-02T03:04:05Z'), '2025-01-02 03:04:05')
        self.assertEqual(lease_manager._format_allocated_at('bad'), 'bad')

    def test_format_lease_status(self):
        now = datetime(2025, 1, 2, 3, 4, 5)
        self.assertEqual(
            lease_manager._format_lease({'expires': '2025-01-02T03:04:04'}, now),
            ('2025-01-02 03:04:04', 'Expired'),
        )
        self.assertEqual(
            lease_manager._format_lease({'expires': '2025-01-02T03:04:06'}, now),
            ('2025-01-02 03:04:06', 'Active'),
        )
        self.assertEqual(lease_manager._format_lease({}, now), ('Unknown', 'Unknown'))


if __name__ == '__main__':
    unittest.main()


class DeleteKeysTests(unittest.TestCase):
    def test_mac_delete_keys(self):
        got = lease_manager._mac_delete_keys('host', 'aabb')
        self.assertEqual(got, [
            '/cluster/nodes/by-hostname/host',
            '/cluster/nodes/by-mac/aabb',
            '/cluster/dhcp/leases/aabb',
        ])

    def test_delete_keys_counts_successes(self):
        client = mock.Mock()
        client.delete.side_effect = [True, False, RuntimeError('down')]
        with mock.patch('builtins.print'):
            self.assertTrue(lease_manager._delete_keys(client, ['a', 'b', 'c'], 'MAC', 'aa'))
        self.assertEqual(client.delete.call_count, 3)


class LeaseMainTests(unittest.TestCase):
    def test_target_detection(self):
        self.assertTrue(lease_manager._looks_like_mac('aa:bb'))
        self.assertTrue(lease_manager._looks_like_mac('aabbccddeeff'))
        self.assertFalse(lease_manager._looks_like_mac('host'))

    def test_delete_target_dispatch(self):
        with mock.patch.object(lease_manager, 'delete_by_mac', return_value=True) as mac, \
                mock.patch.object(lease_manager, 'delete_by_hostname', return_value=False) as host:
            self.assertTrue(lease_manager._delete_target('aa:bb'))
            self.assertFalse(lease_manager._delete_target('host'))
        mac.assert_called_once_with('aa:bb')
        host.assert_called_once_with('host')
