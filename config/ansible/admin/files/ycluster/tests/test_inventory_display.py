"""Display-format tests for inventory show output."""

import io
import unittest
from contextlib import redirect_stdout

from ycluster.cli import inventory


class InventoryDisplayTests(unittest.TestCase):
    def test_print_node_without_hardware(self):
        output = self._render('empty', None, {})
        self.assertIn('=== empty ===', output)
        self.assertIn('[no hardware data', output)
        self.assertNotIn('Vendor:', output)

    def test_print_node_with_hardware_and_asset(self):
        hw = {
            'product': 'Server', 'serial': 'SERIAL', 'cpu': 'EPYC',
            'ram_gb': 80, 'disks': [
                {'name': 'disk', 'size': '512G', 'type': 'ssd', 'model': 'Fast'},
                {'name': 'slow', 'size': '1T', 'type': 'hdd'},
            ],
            'gpus': [{'model': 'A100'}, {'vendor': 'Generic'}],
            'nics': [{'name': 'eth0', 'speed': '25Gb'}, {'name': 'eth1'}],
            'bmc_mac': 'aa', 'os': 'Linux', 'kernel': '6.0',
            'bios_version': '1.0', 'collected_at': '2026-08-30T10:00:00',
        }
        asset = {
            'vendor': 'Vendor', 'purchased_at': '2024-01-01',
            'warranty_expires': '2030-01-01', 'cost': 12.5,
            'cost_currency': 'USD', 'location': 'rack', 'notes': 'keep',
        }
        output = self._render('node-a', hw, asset)
        self.assertIn('Product:   Server', output)
        self.assertIn('RAM:       80 GB', output)
        self.assertIn('disk 512G ssd (Fast)', output)
        self.assertIn('slow 1T hdd', output)
        self.assertIn('GPUs:      A100, Generic', output)
        self.assertIn('NICs:      eth0 25Gb, eth1', output)
        self.assertIn('Collected: 2026-08-30T10:00:00', output)
        self.assertIn('Cost:      $12.50', output)
        self.assertIn('Notes:     keep', output)

    def test_print_node_defaults_and_currency_fallback(self):
        hw = {'disks': [], 'gpus': [], 'nics': []}
        asset = {'cost': 3, 'cost_currency': 'GBP'}
        output = self._render('node', hw, asset)
        self.assertIn('Product:   -', output)
        self.assertIn('Cost:      GBP3.00', output)

    @staticmethod
    def _render(hostname, hw, asset):
        output = io.StringIO()
        with redirect_stdout(output):
            inventory._print_node(hostname, hw, asset)
        return output.getvalue()


if __name__ == '__main__':
    unittest.main()
