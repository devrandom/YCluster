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
                                transit_iface='wg-vm-nat')
        render.assert_called_once_with([], '203.0.113.10', 'eth0', 'wg-vm-nat')
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

    def test_private_range_egress_dropped(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                     '169.254.0.0/16', '100.64.0.0/10'):
            self.assertIn(f'ip daddr {cidr} drop', text)

    def test_validate_exposures_rejects_bad_proto(self):
        with self.assertRaises(ValueError):
            frontend_nat.validate_exposures([
                {'proto': 'sctp', 'ports': '80'}])

    def test_multi_spoke_subnets_rendered_per_host(self):
        exposures = [
            {'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
             'transit_ip': '100.100.2.11'},
            {'proto': 'udp', 'vm': 'vm-b', 'ports': '53',
             'transit_ip': '100.100.3.12'},
        ]
        text = frontend_nat.render_nftables(exposures, '203.0.113.10',
                                            transit_iface='wg-vm-nat')
        self.assertIn('ip saddr 100.100.2.0/24 oifname "eth0" snat', text)
        self.assertIn('ip saddr 100.100.3.0/24 oifname "eth0" snat', text)
        self.assertIn('dnat to 100.100.2.11', text)
        self.assertIn('dnat to 100.100.3.12', text)
        self.assertNotIn('ip saddr 100.100.0.0/24', text)

    def test_rejects_transit_ip_outside_supernet(self):
        with self.assertRaises(ValueError):
            frontend_nat.render_nftables([
                {'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                 'transit_ip': '10.0.0.11'},
            ], '203.0.113.10')

    def test_transit_subnet_derivation(self):
        from ycluster.utils import nat_render
        self.assertEqual(nat_render.transit_subnet('100.100.2.11'),
                         '100.100.2.0/24')
        with self.assertRaises(ValueError):
            nat_render.transit_subnet('not-an-ip')

    def test_private_drops_precede_established(self):
        text = frontend_nat.render_nftables(self.BASE, '203.0.113.10')
        first_drop = text.index('ip daddr 10.0.0.0/8 drop')
        established = text.index('ct state established,related accept')
        self.assertLess(first_drop, established)


class InstanceDetailsTests(unittest.TestCase):
    def test_parses_first_instance(self):
        result = mock.Mock(
            returncode=0,
            stdout='[{"name": "vm-a", "status": "Running"}]')
        incus = mock.Mock(return_value=result)
        with mock.patch.object(frontend_nat, '_incus', incus):
            self.assertEqual(frontend_nat._instance_details('vm-a'),
                             {'name': 'vm-a', 'status': 'Running'})

    def test_tolerates_bad_or_empty_json(self):
        bad = mock.Mock(returncode=0, stdout='not json')
        empty = mock.Mock(returncode=0, stdout='[]')
        incus = mock.Mock(side_effect=[bad, empty])
        with mock.patch.object(frontend_nat, '_incus', incus):
            self.assertEqual(frontend_nat._instance_details('vm-a'), {})
            self.assertEqual(frontend_nat._instance_details('vm-a'), {})


class TransitSyncTests(unittest.TestCase):
    def setUp(self):
        # Most sync tests exercise allocation/convergence. Treat the
        # instance as already transit-only unless a test overrides this.
        patcher = mock.patch.object(
            frontend_nat, '_instance_details',
            return_value={'status': 'Stopped',
                          'expanded_devices': {'eth0': {'type': 'none'}}})
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_adopts_live_device_transactionally(self):
        # vm-a has a live transit0 NIC but no IP recorded in etcd: the
        # sync must claim the live address via CAS and converge records.
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures, live={'vm-a': '100.100.0.13'})
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_claim_transit_ip',
                                  return_value=True) as claim, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus'):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertTrue(changed)
        claim.assert_called_once_with('vm-a', '100.100.0.13')
        set_.assert_called_once_with('vm-a', 'tcp', '80', '100.100.0.13')

    def test_adopt_failure_skips_vm(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures, live={'vm-a': '100.100.0.13'})
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_claim_transit_ip',
                                  return_value=False), \
                mock.patch.object(frontend_nat, 'set_exposure') as set_:
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        set_.assert_not_called()

    def test_valid_transit_ip_rejects_out_of_subnet(self):
        self.assertFalse(frontend_nat._valid_transit_ip('100.100.0.999'))
        self.assertFalse(frontend_nat._valid_transit_ip('10.0.0.5'))
        self.assertFalse(frontend_nat._valid_transit_ip('not-an-ip'))
        self.assertTrue(frontend_nat._valid_transit_ip('100.100.0.254'))
        self.assertTrue(frontend_nat._valid_transit_ip('100.100.2.254'))

    def test_configured_host_subnet_is_used(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch('ycluster.utils.vm_manager._host_config_value',
                           return_value='100.100.2.0/24'), \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value='100.100.2.11') as reserve, \
                mock.patch.object(frontend_nat, 'set_exposure'), \
                mock.patch.object(frontend_nat, '_incus'):
            frontend_nat.sync_transit_nics({'vm-a'})
        reserve.assert_called_once_with('vm-a', '100.100.2.0/24')

    def test_legacy_record_is_migrated_and_released(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': '100.100.0.11'}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value='100.100.2.11') as reserve, \
                mock.patch.object(frontend_nat, '_release_transit_ip') as rel, \
                mock.patch.object(frontend_nat, '_disable_default_nic',
                                  return_value=False), \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus') as incus:
            changed = frontend_nat.sync_transit_nics(
                {'vm-a'}, '100.100.2.0/24')
        self.assertTrue(changed)
        reserve.assert_called_once_with('vm-a', '100.100.2.0/24')
        incus.assert_any_call('config', 'device', 'set', 'vm-a', 'transit0',
                              'ipv4.address=100.100.2.11', check=True)
        rel.assert_called_once_with('100.100.0.11')
        set_.assert_called_once_with('vm-a', 'tcp', '80', '100.100.2.11')

    def test_legacy_migration_skips_when_pool_exhausted(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': '100.100.0.11'}]
        patches = self._sync_patches(exposures)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value=None), \
                mock.patch.object(frontend_nat, '_release_transit_ip') as rel, \
                mock.patch.object(frontend_nat, 'set_exposure') as set_:
            changed = frontend_nat.sync_transit_nics(
                {'vm-a'}, '100.100.2.0/24')
        self.assertFalse(changed)
        rel.assert_not_called()
        set_.assert_not_called()

    def test_legacy_live_device_is_migrated(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        patches = self._sync_patches(exposures, live={'vm-a': '100.100.0.11'})
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_reserve_transit_ip',
                                  return_value='100.100.2.12') as reserve, \
                mock.patch.object(frontend_nat, '_release_transit_ip') as rel, \
                mock.patch.object(frontend_nat, '_disable_default_nic',
                                  return_value=False), \
                mock.patch.object(frontend_nat, 'set_exposure') as set_, \
                mock.patch.object(frontend_nat, '_incus') as incus:
            changed = frontend_nat.sync_transit_nics(
                {'vm-a'}, '100.100.2.0/24')
        self.assertTrue(changed)
        reserve.assert_called_once_with('vm-a', '100.100.2.0/24')
        incus.assert_any_call('config', 'device', 'set', 'vm-a', 'transit0',
                              'ipv4.address=100.100.2.12', check=True)
        rel.assert_called_once_with('100.100.0.11')
        set_.assert_called_once_with('vm-a', 'tcp', '80', '100.100.2.12')

    def test_transit_only_nic_removes_default_route(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': '100.100.2.11'}]
        patches = self._sync_patches(exposures)
        details = {'status': 'Running',
                   'expanded_devices': {'eth0': {'type': 'nic'}}}
        incus = mock.Mock(returncode=0)
        with patches[0], patches[1], \
                mock.patch.object(frontend_nat, '_instance_details',
                                  return_value=details), \
                mock.patch.object(frontend_nat, '_incus', incus):
            changed = frontend_nat.sync_transit_nics(
                {'vm-a'}, '100.100.2.0/24')
        self.assertTrue(changed)
        incus.assert_any_call('config', 'device', 'override', 'vm-a',
                              'eth0', 'type=none', check=False)
        incus.assert_any_call('restart', 'vm-a', check=True)

    def test_default_nic_falls_back_to_set_for_existing_override(self):
        override = mock.Mock(returncode=1)
        incus = mock.Mock(return_value=override)
        details = {'expanded_devices': {'eth0': {'type': 'nic'}}}
        with mock.patch.object(frontend_nat, '_incus', incus):
            self.assertTrue(frontend_nat._disable_default_nic('vm-a', details))
        incus.assert_any_call('config', 'device', 'set', 'vm-a', 'eth0',
                              'type=none', check=True)

    def test_reservation_keys_are_legacy_and_per_subnet(self):
        self.assertEqual(
            frontend_nat._transit_reservation_key('100.100.0.11'),
            b'/cluster/frontend/nat/_ip/100.100.0.11')
        self.assertEqual(
            frontend_nat._transit_reservation_key('100.100.2.11'),
            b'/cluster/frontend/nat/_ip/100.100.2.0/24/100.100.2.11')

    def test_claim_uses_per_subnet_key(self):
        client = mock.Mock()
        client.transaction.return_value = (True, [])
        with _patched_etcd(client):
            self.assertTrue(frontend_nat._claim_transit_ip(
                'vm-a', '100.100.2.11'))
        client.transactions.put.assert_called_once_with(
            b'/cluster/frontend/nat/_ip/100.100.2.0/24/100.100.2.11',
            b'vm-a')

    def test_release_deletes_per_subnet_key(self):
        client = mock.Mock()
        with _patched_etcd(client):
            frontend_nat._release_transit_ip('100.100.2.11')
        client.delete.assert_called_once_with(
            b'/cluster/frontend/nat/_ip/100.100.2.0/24/100.100.2.11')

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
        client.transaction.return_value = (False, [])
        with _patched_etcd(client):
            self.assertIsNone(frontend_nat._reserve_transit_ip(
                'vm-a', frontend_nat.LEGACY_TRANSIT_SUBNET))

    def test_reserve_wins_first_candidate(self):
        client = mock.Mock()
        client.transaction.return_value = (True, [])
        with _patched_etcd(client):
            self.assertEqual(frontend_nat._reserve_transit_ip(
                'vm-a', frontend_nat.LEGACY_TRANSIT_SUBNET),
                             '100.100.0.11')

    def test_reserve_uses_per_subnet_keys(self):
        client = mock.Mock()
        client.transaction.return_value = (True, [])
        with _patched_etcd(client):
            frontend_nat._reserve_transit_ip('vm-a', '100.100.2.0/24')
        client.transactions.put.assert_called_once_with(
            b'/cluster/frontend/nat/_ip/100.100.2.0/24/100.100.2.11',
            b'vm-a')

    def test_reserve_uses_etcd_transaction(self):
        client = mock.Mock()
        client.transaction.return_value = (True, [])
        with _patched_etcd(client):
            frontend_nat._reserve_transit_ip(
                'vm-a', frontend_nat.LEGACY_TRANSIT_SUBNET)
        client.transaction.assert_called_once()
        kwargs = client.transaction.call_args[1]
        self.assertIn('compare', kwargs)
        self.assertIn('success', kwargs)

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
