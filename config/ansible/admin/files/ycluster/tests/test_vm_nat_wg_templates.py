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
        self.assertIn('ip rule add from 100.100.2.0/24 iif transit0 table 100', text)
        self.assertIn(
            'ip rule add from 100.100.2.0/24 iif transit0 lookup main '
            'suppress_prefixlength 0 priority 90', text)
        self.assertIn('Endpoint = 203.0.113.10:51820', text)

    def test_spoke_firewall_drops_spoofed_sources(self):
        text = self._render(
            'vm-nat-spoke-firewall.nft.j2',
            vm_transit_subnet='100.100.2.0/24')
        # Forward: only this spoke's /24 may leave via the overlay.
        self.assertIn('iifname "transit0" oifname "wg-vm-nat" '
                      'ip saddr 100.100.2.0/24 accept', text)
        # Input: DHCP and DNS are allowed; every other transit0 packet
        # (including spoofed cluster sources) is dropped, and the input
        # drop comes after the service allows.
        self.assertIn('iifname "transit0" udp dport 67 accept', text)
        self.assertIn('iifname "transit0" ip saddr 100.100.2.0/24 '
                      'udp dport 53 accept', text)
        self.assertIn('iifname "transit0" ip saddr 100.100.2.0/24 '
                      'tcp dport 53 accept', text)
        drop = text.index('iifname "transit0" drop')
        dns = text.index('tcp dport 53 accept')
        self.assertLess(dns, drop)
        self.assertIn('iifname "transit0" drop', text)
        # Management return path: established replies to the bastion are
        # allowed before the drop; guest-initiated flows are not.
        self.assertIn(
            'iifname "transit0" oifname "incusbr0" '
            'ip saddr 100.100.2.0/24 ct state established,related accept',
            text)


    def test_transit_acl_does_not_shadow_local_allow(self):
        # Incus orders ACL rules by action (drop before allow), not list
        # order. Any drop whose destination contains the local /24 would
        # therefore shadow the local-transit allow.
        acl_root = (Path(__file__).resolve().parents[3] / 'templates')
        source = (acl_root / 'vm-transit-acl.yml.j2').read_text()
        text = Environment().from_string(source).render(
            vm_transit_subnet='100.100.113.0/24')
        self.assertIn('destination: 100.100.113.0/24', text)
        self.assertNotIn('100.64.0.0/10', text)
        self.assertNotIn('100.100.0.0/16', text)
        for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                     '169.254.0.0/16'):
            self.assertIn(f'destination: {cidr}', text)

    def test_playbook_family_check_matches_template(self):
        # The existing-forward-chain guard must look for the same table
        # family the template installs, or re-runs fail spuriously.
        overlay = (Path(__file__).resolve().parents[4] / 'app' /
                   'install-vm-nat-wg.yml').read_text()
        self.assertIn("table inet ycluster_vm_nat_spoke", overlay)
        self.assertNotIn("table ip ycluster_vm_nat_spoke", overlay)

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
            # YAML single quotes do not process backslash escapes: a
            # double backslash reaches the regex engine as a literal
            # backslash and the substitution silently never matches.
            self.assertNotIn(r"regex_replace('^100\\.100\\.', '')", text)
            self.assertNotIn(r"match('^100\\.100\\.", text)
