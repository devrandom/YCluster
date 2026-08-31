"""Tests for the inventory CSV export command."""

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ycluster.cli import inventory


class InventoryExportTests(unittest.TestCase):
    def test_export_stdout_and_row_shape(self):
        rows = [
            {'allocation': None, 'hardware': None, 'asset': None},
            self._row(),
        ]
        with mock.patch.object(inventory.inv, 'list_all', return_value=rows), \
                redirect_stdout(io.StringIO()) as output:
            inventory._export(SimpleNamespace(output=None))
        rendered = output.getvalue()
        parsed = list(csv.reader(io.StringIO(rendered)))
        self.assertEqual(parsed[0][0], 'hostname')
        self.assertEqual(parsed[-1][0], 'node-a')
        self.assertIn('disk-a 1T ssd', parsed[-1])
        self.assertIn('Fast', parsed[-1])

    def test_export_to_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'inventory.csv'
            with mock.patch.object(inventory.inv, 'list_all', return_value=[]), \
                    redirect_stdout(io.StringIO()) as output:
                inventory._export(SimpleNamespace(output=str(path)))
            self.assertTrue(path.exists())
            self.assertIn(str(path), output.getvalue())

    @staticmethod
    def _row():
        return {
            'allocation': {'hostname': 'node-a', 'type': 'compute',
                           'ip': 'ip-a'},
            'hardware': {
                'product': 'Server', 'serial': 'serial-a',
                'bios_version': '1.0', 'cpu': 'EPYC', 'ram_gb': 80,
                'disks': [{'name': 'disk-a', 'size': '1T', 'type': 'ssd'}],
                'gpus': [{'model': 'Fast'}],
                'nics': [{'name': 'eth0', 'speed': '25Gb'}],
                'os': 'Linux', 'kernel': '6.0',
                'collected_at': '2026-08-30T10:00:00',
            },
            'asset': {
                'vendor': 'Vendor', 'purchased_at': '2024-01-01',
                'warranty_expires': '2030-01-01', 'cost': 12.5,
                'cost_currency': 'USD', 'location': 'rack', 'notes': 'keep',
                'updated_at': '2026-08-30T10:01:00',
            },
        }


if __name__ == '__main__':
    unittest.main()
