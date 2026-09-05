import json
import unittest
from unittest import mock

from ycluster.cli import frontend as frontend_nat_cli
from ycluster.utils import frontend_nat
from ycluster.common import etcd_utils


def _patched_etcd(client):
    return mock.patch.object(etcd_utils, 'get_etcd_client',
                             return_value=client)


def _fake_client(keys):
    client = mock.Mock()

    def get_prefix(prefix):
        for key, val in keys.items():
            yield val.encode(), mock.Mock(key=key.encode())

    client.get_prefix = get_prefix
    return client


class ParsePortsTests(unittest.TestCase):
    def test_single_and_range(self):
        self.assertEqual(frontend_nat._parse_ports('80'), (80, 80))
        self.assertEqual(frontend_nat._parse_ports('20000-20099'), (20000, 20099))

    def test_invalid(self):
        for bad in ('', 'abc', '0', '65536', '200-100', '80-'):
            with self.assertRaises(ValueError, msg=bad):
                frontend_nat._parse_ports(bad)


class ValidateExposuresTests(unittest.TestCase):
    def test_overlap_same_proto(self):
        with self.assertRaises(ValueError):
            frontend_nat.validate_exposures([
                {'proto': 'tcp', 'ports': '20000-20099'},
                {'proto': 'tcp', 'ports': '20050-20060'},
            ])

    def test_no_overlap_across_protos(self):
        frontend_nat.validate_exposures([
            {'proto': 'tcp', 'ports': '20000-20099'},
            {'proto': 'udp', 'ports': '20000-20099'},
        ])


class SetExposureTests(unittest.TestCase):
    def test_set_writes_key(self):
        client = mock.Mock()
        with _patched_etcd(client):
            key = frontend_nat.set_exposure('vm-a', 'tcp', '20000-20099',
                                            '100.100.0.11')
        self.assertEqual(key, '/cluster/frontend/nat/tcp/vm-a/20000-20099')
        client.put.assert_called_once_with(
            key, json.dumps({'transit_ip': '100.100.0.11'}))

    def test_set_rejects_bad_proto_and_ip(self):
        with _patched_etcd(mock.Mock()):
            with self.assertRaises(ValueError):
                frontend_nat.set_exposure('vm-a', 'sctp', '80')
            with self.assertRaises(ValueError):
                frontend_nat.set_exposure('vm-a', 'tcp', '80', '10.0.0.1')


class ListExposuresTests(unittest.TestCase):
    def test_list_parses_keys(self):
        keys = {
            '/cluster/frontend/nat/tcp/vm-a/20000-20099':
                json.dumps({'transit_ip': '100.100.0.11'}),
            '/cluster/frontend/nat/udp/vm-b/80-80':
                json.dumps({}),
        }
        with _patched_etcd(_fake_client(keys)):
            got = frontend_nat.list_exposures()
        self.assertEqual(got, [
            {'proto': 'tcp', 'vm': 'vm-a', 'ports': '20000-20099',
             'transit_ip': '100.100.0.11'},
            {'proto': 'udp', 'vm': 'vm-b', 'ports': '80-80',
             'transit_ip': ''},
        ])

    def test_list_skips_malformed_records(self):
        keys = {
            '/cluster/frontend/nat/nodashes': 'not json',
            '/cluster/frontend/nat/tcp/vm-a/20000-20099': '{bad',
            # Injection-style records must be quarantined, not rendered.
            '/cluster/frontend/nat/sctp/vm-x/80-80': '{}',
            '/cluster/frontend/nat/tcp/vm-x/99999-99999': '{}',
            '/cluster/frontend/nat/tcp/vm-x/80-80':
                '{"transit_ip": "10.0.0.1"}',
        }
        with _patched_etcd(_fake_client(keys)):
            self.assertEqual(frontend_nat.list_exposures(), [])

    def test_delete_rejects_bad_proto(self):
        with self.assertRaises(ValueError):
            frontend_nat.delete_exposure('vm-a', 'sctp', '80')

    def test_set_rejects_bad_vm_name(self):
        with _patched_etcd(mock.Mock()):
            with self.assertRaises(ValueError):
                frontend_nat.set_exposure('Bad Name!', 'tcp', '80')


class CliTests(unittest.TestCase):
    def _run(self, fn, **kwargs):
        args = mock.Mock(**kwargs)
        with mock.patch('builtins.print') as print_:
            fn(args)
        return print_

    def test_expose(self):
        with mock.patch.object(frontend_nat, 'set_exposure',
                               return_value='key') as set_:
            self._run(frontend_nat_cli.frontend_expose,
                      vm='vm-a', proto='tcp', ports='80',
                      transit_ip='100.100.0.11')
        set_.assert_called_once_with('vm-a', 'tcp', '80', '100.100.0.11')

    def test_unexpose(self):
        with mock.patch.object(frontend_nat, 'delete_exposure') as delete:
            self._run(frontend_nat_cli.frontend_unexpose,
                      vm='vm-a', proto='tcp', ports='80')
        delete.assert_called_once_with('vm-a', 'tcp', '80')

    def test_exposures_empty(self):
        with mock.patch.object(frontend_nat, 'list_exposures',
                               return_value=[]):
            printed = self._run(frontend_nat_cli.frontend_exposures)
        printed.assert_called_once_with('No NAT exposures configured.')

    def test_exposures_list(self):
        rows = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80-80',
                 'transit_ip': '100.100.0.11'}]
        with mock.patch.object(frontend_nat, 'list_exposures',
                               return_value=rows):
            printed = self._run(frontend_nat_cli.frontend_exposures)
        self.assertIn('vm-a', printed.call_args[0][0])

    def test_render_nft(self):
        with mock.patch.object(frontend_nat, 'list_exposures', return_value=[]), \
                mock.patch.object(frontend_nat, 'render_nftables',
                                  return_value='RULES\n') as render:
            printed = self._run(frontend_nat_cli.frontend_render_nft,
                                public_ip='203.0.113.10', wan_iface='eth0',
                                transit_iface='transit0')
        render.assert_called_once_with([], '203.0.113.10', 'eth0', 'transit0')
        printed.assert_called_once_with('RULES\n', end='')


class RenderNftablesTests(unittest.TestCase):
    BASE = [
        {'proto': 'tcp', 'vm': 'vm-a', 'ports': '20000-20099',
         'transit_ip': '100.100.0.11'},
    ]

    def test_forward_policy_is_drop(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        self.assertIn('policy drop;', text)
        self.assertIn('chain forward', text)

    def test_dnat_and_snat_rendered(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        self.assertIn('tcp dport 20000-20099 dnat to 100.100.0.11', text)
        self.assertIn('snat ip to 203.0.113.10', text)

    def test_cluster_egress_dropped(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        self.assertIn('ip daddr 10.0.0.0/8 drop', text)

    def test_unallocated_mapping_skipped(self):
        exposures = self.BASE + [
            {'proto': 'tcp', 'vm': 'vm-b', 'ports': '80',
             'transit_ip': ''},
        ]
        text = frontend_nat.render_nftables(exposures, '203.0.113.10')
        self.assertNotIn('vm-b', text)
        self.assertIn('dnat to 100.100.0.11', text)

    def test_overlap_refused(self):
        with self.assertRaises(ValueError):
            frontend_nat.render_nftables(
                self.BASE + self.BASE, '203.0.113.10')

    def test_render_is_deterministic(self):
        reversed_list = list(reversed(self.BASE))
        self.assertEqual(
            frontend_nat.render_nftables(self.BASE, '203.0.113.10'),
            frontend_nat.render_nftables(reversed_list, '203.0.113.10'))

    def test_atomic_replace_header(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        self.assertTrue(text.startswith(
            'table ip ycluster_vm_nat\n'
            'delete table ip ycluster_vm_nat\n'
            'table ip ycluster_vm_nat {'))

    def test_egress_is_per_protocol_not_blanket(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        self.assertIn('tcp accept', text)
        self.assertIn('udp accept', text)
        self.assertIn('icmp ct state established,related accept', text)
        self.assertNotIn('ip saddr 100.100.0.0/24 accept', text)


class TransitSyncTests(unittest.TestCase):
    def _sync_patches(self, exposures, live=None):
        """Patch list_exposures + _transit_nics_by_vm for a sync run."""
        return [
            mock.patch.object(frontend_nat, 'list_exposures',
                              return_value=exposures),
            mock.patch.object(frontend_nat, '_transit_nics_by_vm',
                              return_value=live or {}),
        ]

    def test_allocates_and_persists(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures)
        incus = mock.Mock()
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value='100.100.0.11'), \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus', incus):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertTrue(changed)
        set_.assert_called_once_with('vm-a', 'tcp', '80', '100.100.0.11')
        incus.assert_called_once()

    def test_skips_nonlocal_vms(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-other', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip') as r, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus'):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        r.assert_not_called()
        set_.assert_not_called()

    def test_idempotent_when_already_allocated(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': '100.100.0.11'}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip') as r, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus') as incus:
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        r.assert_not_called()
        set_.assert_not_called()
        incus.assert_not_called()

    def test_converges_partial_records(self):
        # Two records on the same VM, only the first has an IP: the
        # empty one must be filled in (partial-write convergence).
        exposures = [
            {'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
             'transit_ip': '100.100.0.11'},
            {'proto': 'udp', 'vm': 'vm-a', 'ports': '90',
             'transit_ip': ''},
        ]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip') as r, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus'):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertTrue(changed)
        set_.assert_called_once_with('vm-a', 'udp', '90', '100.100.0.11')
        r.assert_not_called()

    def test_stale_live_device_blocks_its_ip(self):
        # vm-b holds a live transit NIC but has no exposure records; the
        # allocation for vm-a must not pick vm-b's live IP. (Reservation
        # is etcd-transactional, so we assert the incus add carries the
        # reserved IP rather than the live collision.)
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures, live={'vm-b': '100.100.0.11'})
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value='100.100.0.12'), \
                mock.patch.object(frontend_nat, 'set_exposure'), \
                mock.patch.object(frontend_nat, '_incus') as incus:
            frontend_nat.sync_transit_nics({'vm-a'})
        self.assertIn('ipv4.address=100.100.0.12', incus.call_args[0])

    def test_valid_transit_ip_rejects_out_of_subnet(self):
        self.assertFalse(frontend_nat._valid_transit_ip('100.100.0.999'))
        self.assertFalse(frontend_nat._valid_transit_ip('10.0.0.5'))
        self.assertFalse(frontend_nat._valid_transit_ip('not-an-ip'))
        self.assertTrue(frontend_nat._valid_transit_ip('100.100.0.254'))

    def test_list_quarantines_bad_vm_name_and_ip(self):
        keys = {
            '/cluster/frontend/nat/tcp/vm_bad!name/80-80': '{}',
            '/cluster/frontend/nat/tcp/vm-ok/80-80':
                '{"transit_ip": "100.100.0.999"}',
        }
        with _patched_etcd(_fake_client(keys)):
            self.assertEqual(frontend_nat.list_exposures(), [])

    def test_delete_exposure(self):
        client = mock.Mock()
        with _patched_etcd(client):
            frontend_nat.delete_exposure('vm-a', 'tcp', '80')
        client.delete.assert_called_once_with(
            '/cluster/frontend/nat/tcp/vm-a/80-80')

    def test_reserve_exhausted_pool_returns_none(self):
        client = mock.Mock()
        client.transactions.put.return_value = False
        client.transactions.version.return_value = mock.ANY
        with _patched_etcd(client):
            self.assertIsNone(frontend_nat._reserve_transit_ip('vm-a'))

    def test_reserve_wins_first_candidate(self):
        client = mock.Mock()
        client.transactions.put.return_value = True
        client.transactions.version.return_value = mock.ANY
        with _patched_etcd(client):
            self.assertEqual(frontend_nat._reserve_transit_ip('vm-a'),
                             '100.100.0.11')

    def test_conflicting_transit_ips_skips_vm(self):
        exposures = [
            {'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
             'transit_ip': '100.100.0.11'},
            {'proto': 'udp', 'vm': 'vm-a', 'ports': '90',
             'transit_ip': '100.100.0.99'},
        ]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip') as r, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_:
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        r.assert_not_called()
        set_.assert_not_called()

    def test_pool_exhaustion_warns_and_continues(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value=None), \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus'):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        set_.assert_not_called()

    def test_transit_nics_by_vm_parses(self):
        stdout = json.dumps([
            {'name': 'vm-a',
             'devices': {'transit0': {'ipv4.address': '100.100.0.11'}}},
            {'name': 'vm-b', 'devices': {}},
            {'name': 'vm-c', 'devices': {'transit0': {}}},
        ])
        incus = mock.Mock(return_value=mock.Mock(stdout=stdout))
        with mock.patch.object(frontend_nat, '_incus', incus):
            self.assertEqual(frontend_nat._transit_nics_by_vm(),
                             {'vm-a': '100.100.0.11'})

    def test_transit_nics_by_vm_tolerates_bad_json(self):
        incus = mock.Mock(return_value=mock.Mock(stdout='not json'))
        with mock.patch.object(frontend_nat, '_incus', incus):
            self.assertEqual(frontend_nat._transit_nics_by_vm(), {})
