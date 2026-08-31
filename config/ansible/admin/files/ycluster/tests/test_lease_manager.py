import unittest
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
