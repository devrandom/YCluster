"""Tests for the snmp CLI helpers."""

import unittest

from ycluster.cli import snmp


class FakeClient:
    """Minimal etcd client stand-in."""

    def __init__(self, allocations):
        self._allocations = [
            (alloc_bytes, None) for alloc_bytes in allocations
        ]

    def get_prefix(self, prefix):
        return self._allocations


def _alloc(hostname, ip, mac='', device_type='pdu'):
    import json
    return json.dumps({
        'hostname': hostname,
        'ip': ip,
        'mac': mac,
        'type': device_type,
    }).encode()


class ResolveDeviceTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient([
            _alloc('pdu1', '10.0.0.141', '00:11:22:33:44:55'),
            _alloc('s1', '10.0.0.11', device_type='storage'),
        ])

    def test_list_filters_to_pdus(self):
        devices = snmp._list_devices(self.client)
        self.assertEqual([d['hostname'] for d in devices], ['pdu1'])

    def test_resolve_by_hostname(self):
        device = snmp._resolve_device(self.client, 'pdu1')
        self.assertEqual(device['ip'], '10.0.0.141')

    def test_resolve_by_fqdn(self):
        device = snmp._resolve_device(self.client, 'pdu1.xc')
        self.assertEqual(device['ip'], '10.0.0.141')

    def test_resolve_by_ip(self):
        device = snmp._resolve_device(self.client, '10.0.0.141')
        self.assertEqual(device['hostname'], 'pdu1')

    def test_resolve_unknown(self):
        self.assertIsNone(snmp._resolve_device(self.client, 'pdu2'))

    def test_resolve_ignores_non_pdu(self):
        self.assertIsNone(snmp._resolve_device(self.client, 's1'))


if __name__ == '__main__':
    unittest.main()
