import unittest
from pathlib import Path

from jinja2 import Environment


class WireGuardTemplateTests(unittest.TestCase):
    def _render(self, relpath, **context):
        root = Path(__file__).resolve().parents[4] / 'app' / 'templates'
        source = (root / relpath).read_text()
        return Environment().from_string(source).render(**context)

    def test_hub_allowed_ips_are_exact_per_spoke(self):
        text = self._render(
            'vm-nat-wg-hub.conf.j2',
            vm_nat_wg_port='51820',
            hub_private_key='hub-key',
            vm_nat_participating_spokes=[
                {'host': 'spoke-a', 'index': 2},
                {'host': 'spoke-b', 'index': 3},
            ],
            hostvars={
                'spoke-a': {
                    'vm_nat_wg_public_key': 'key-a',
                    'vm_nat_wg_address': '100.101.0.3',
                    'vm_transit_subnet': '100.100.2.0/24',
                },
                'spoke-b': {
                    'vm_nat_wg_public_key': 'key-b',
                    'vm_nat_wg_address': '100.101.0.4',
                    'vm_transit_subnet': '100.100.3.0/24',
                },
            },
        )
        self.assertIn('MTU = 1420', text)
        self.assertIn('AllowedIPs = 100.101.0.3/32, 100.100.2.0/24', text)
        self.assertIn('AllowedIPs = 100.101.0.4/32, 100.100.3.0/24', text)
        self.assertNotIn('10.0.', text)

    def test_spoke_policy_routes_only_transit_source(self):
        text = self._render(
            'vm-nat-wg-spoke.conf.j2',
            vm_nat_wg_address='100.101.0.3',
            vm_nat_wg_private_key='spoke-key',
            vm_transit_subnet='100.100.2.0/24',
            vm_nat_frontend_host='frontend-a',
            hostvars={
                'frontend-a': {
                    'hub_public_key': type('R', (), {'stdout': 'hub-key\n'})(),
                    'nat_public_ip': '203.0.113.10',
                },
            },
        )
        self.assertIn('MTU = 1420', text)
        self.assertIn('ip rule add from 100.100.2.0/24 table 100', text)
        self.assertIn('Endpoint = 203.0.113.10:51820', text)

    def test_spoke_firewall_drops_spoofed_sources(self):
        root = Path(__file__).resolve().parents[4] / 'app' / 'templates'
        text = (root / 'vm-nat-spoke-firewall.nft.j2').read_text()
        self.assertIn('ip saddr {{ vm_transit_subnet }} accept', text)
        self.assertIn('iifname "transit0" drop', text)

    def test_class_octet_derivation_is_synced(self):
        tests_root = Path(__file__).resolve().parents[2]
        admin_root = tests_root.parents[1]
        overlay = (admin_root / 'app' / 'install-vm-nat-wg.yml').read_text()
        incus = (admin_root / 'admin' / 'install-incus.yml').read_text()
        derivation = "default(110 if inventory_hostname.startswith('nv') else 50)"
        for text in (overlay, incus):
            self.assertIn(derivation, text)
            self.assertIn('vm_transit_class_octet | int + '
                          'vm_nat_transit_index | int', text)
