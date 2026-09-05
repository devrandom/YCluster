import json
import unittest
from unittest import mock

from ycluster.cli import frontend as frontend_nat_cli
from ycluster.utils import frontend_nat


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
        with mock.patch.object(frontend_nat, 'get_etcd_client', return_value=client):
            key = frontend_nat.set_exposure('vm-a', 'tcp', '20000-20099',
                                            '100.100.0.11')
        self.assertEqual(key, '/cluster/frontend/nat/tcp/vm-a/20000-20099')
        client.put.assert_called_once_with(
            key, json.dumps({'transit_ip': '100.100.0.11'}))

    def test_set_rejects_bad_proto_and_ip(self):
        with mock.patch.object(frontend_nat, 'get_etcd_client'):
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
        with mock.patch.object(frontend_nat, 'get_etcd_client',
                               return_value=_fake_client(keys)):
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
        }
        with mock.patch.object(frontend_nat, 'get_etcd_client',
                               return_value=_fake_client(keys)):
            self.assertEqual(frontend_nat.list_exposures(), [])

    def test_delete_rejects_bad_proto(self):
        with self.assertRaises(ValueError):
            frontend_nat.delete_exposure('vm-a', 'sctp', '80')

    def test_set_rejects_bad_vm_name(self):
        with mock.patch.object(frontend_nat, 'get_etcd_client'):
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


class TransitSyncTests(unittest.TestCase):
    def _set_exposures(self, exposures):
        """Mock list_exposures/set_exposure for a sync run."""
        stored = {e['vm']: e for e in exposures}
        patches = [
            mock.patch.object(frontend_nat, 'list_exposures',
                              return_value=exposures),
            mock.patch.object(frontend_nat, 'set_exposure',
                              side_effect=lambda vm, p, ports, ip=None:
                                  stored[vm].__setitem__('transit_ip', ip)),
            mock.patch.object(frontend_nat, '_transit_nics_by_vm',
                              return_value={}),
        ]
        return stored, patches

    def test_allocates_and_persists(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': ''}]
        stored, patches = self._set_exposures(exposures)
        incus = mock.Mock()
        with patches[0], patches[1], patches[2], \
                mock.patch.object(frontend_nat, '_incus', incus):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertTrue(changed)
        self.assertEqual(stored['vm-a']['transit_ip'], '100.100.0.11')

    def test_skips_nonlocal_vms(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-other', 'ports': '80',
                      'transit_ip': ''}]
        stored, patches = self._set_exposures(exposures)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(frontend_nat, '_incus'):
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        self.assertEqual(stored['vm-other']['transit_ip'], '')

    def test_idempotent_when_already_allocated(self):
        exposures = [{'proto': 'tcp', 'vm': 'vm-a', 'ports': '80',
                      'transit_ip': '100.100.0.11'}]
        _, patches = self._set_exposures(exposures)
        with patches[0], patches[1], patches[2], \
                mock.patch.object(frontend_nat, '_incus') as incus:
            changed = frontend_nat.sync_transit_nics({'vm-a'})
        self.assertFalse(changed)
        incus.assert_not_called()

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
