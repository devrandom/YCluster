import unittest

import app_inventory


class InventoryCsvTests(unittest.TestCase):
    def test_renders_sorted_rows(self):
        rows = [
            {'allocation': {'hostname': 'b', 'type': 'vm', 'ip': 'ip-b'},
             'hardware': {'gpus': [{'model': 'gpu'}], 'disks': [{'name': 'sda', 'size': '1G', 'type': 'ssd'}]},
             'asset': {}},
            {'allocation': {'hostname': 'a', 'type': 'storage', 'ip': 'ip-a'},
             'hardware': {'nics': [{'name': 'eth0', 'speed': '25G'}]},
             'asset': {'cost_currency': 'USD'}},
        ]
        out = app_inventory.export_csv(rows).getvalue()
        lines = out.splitlines()
        self.assertEqual(lines[0], ','.join(app_inventory.FIELDS))
        self.assertIn('a,storage,ip-a', lines[1])
        self.assertIn('b,vm,ip-b', lines[2])
        self.assertIn('eth0 25G', out)
        self.assertIn('sda 1G ssd', out)

    def test_missing_values_default(self):
        out = app_inventory.export_csv([{'allocation': None, 'hardware': None, 'asset': None}]).getvalue()
        self.assertIn('EUR', out)


if __name__ == '__main__':
    unittest.main()
