"""Tests for pin_existing_vms helper functions."""

import subprocess
import unittest
from unittest import mock

from ycluster.utils import vm_ip_pinning as vm
from ycluster.utils import vm_manager


def _inst(name, ip=None):
    state = {'network': {
        'eth0': {'addresses': [
            {'family': 'inet', 'scope': 'global', 'address': ip} if ip
            else {'family': 'inet6', 'scope': 'link', 'address': 'fe80::1'}
        ]}}}
    return {'name': name, 'state': state}


class LiveIPv4Tests(unittest.TestCase):
    def test_returns_global_inet(self):
        inst = _inst('vm1', 'ip-a')
        self.assertEqual(vm._live_ipv4(inst), 'ip-a')

    def test_returns_none_when_stopped(self):
        inst = _inst('vm1')
        self.assertIsNone(vm._live_ipv4(inst))

class LiveIPCountsTests(unittest.TestCase):
    def test_counts_collisions(self):
        insts = [_inst('a', 'ip-a'), _inst('b', 'ip-a'),
                 _inst('c', 'ip-b'), _inst('d')]
        counts = vm._live_ip_counts(insts)
        self.assertEqual(counts, {'ip-a': 2, 'ip-b': 1})


class PinQueuesTests(unittest.TestCase):
    def test_unique_first_collision_later(self):
        insts = [_inst('a', 'ip-a'), _inst('b', 'ip-a'),
                 _inst('c', 'ip-b')]
        counts = vm._live_ip_counts(insts)
        first, later = vm._pin_queues(insts, {}, counts)
        self.assertEqual(first, ['c'])
        self.assertEqual(later, ['a', 'b'])

    def test_skips_bastion_and_pinned(self):
        insts = [_inst('bastion', 'ip-a'), _inst('pinned', 'ip-b')]
        counts = vm._live_ip_counts(insts)
        first, later = vm._pin_queues(
            insts, {'pinned': 'ip-b'}, counts)
        self.assertEqual(first, [])
        self.assertEqual(later, [])

if __name__ == '__main__':
    unittest.main()


class ExtractLifecycleInstanceTests(unittest.TestCase):
    def _line(self, action='instance-started', source='/1.0/instances/vm1'):
        import json as _json
        return _json.dumps({'metadata': {'action': action, 'source': source}})

    def test_started(self):
        action, name = vm_manager._extract_lifecycle_instance(self._line())
        self.assertEqual(action, 'instance-started')
        self.assertEqual(name, 'vm1')

    def test_restarted(self):
        action, name = vm_manager._extract_lifecycle_instance(
            self._line(action='instance-restarted'))
        self.assertEqual(action, 'instance-restarted')
        self.assertEqual(name, 'vm1')

    def test_irrelevant_action(self):
        action, name = vm_manager._extract_lifecycle_instance(
            self._line(action='instance-shutdown'))
        self.assertIsNone(action)
        self.assertIsNone(name)

    def test_bad_json(self):
        action, name = vm_manager._extract_lifecycle_instance('not json')
        self.assertIsNone(action)
        self.assertIsNone(name)

    def test_bad_source_path(self):
        action, name = vm_manager._extract_lifecycle_instance(
            self._line(source='/1.0/containers/vm1'))
        self.assertIsNone(action)
        self.assertIsNone(name)

class IncusRunnerTests(unittest.TestCase):
    def test_incus_never_inherits_stdin_and_raises_on_failure(self):
        with mock.patch.object(vm.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='{}')
            self.assertEqual(vm._incus_json('list', '--format', 'json'), {})
            self.assertIs(run.call_args.kwargs['stdin'],
                          subprocess.DEVNULL)
            run.return_value = mock.Mock(returncode=1, stdout='', stderr='boom')
            with self.assertRaisesRegex(RuntimeError, 'incus list .* boom'):
                vm._incus('list')

    def test_incus_with_stdin(self):
        with mock.patch.object(vm.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='')
            vm._incus('exec', 'vm1', '--', 'true', stdin='data')
            self.assertEqual(run.call_args.kwargs['input'], 'data')


class PinExistingVmsTests(unittest.TestCase):
    def _inst(self, name, ip=None):
        state = {'network': {'eth0': {'addresses': [
            {'family': 'inet', 'scope': 'global', 'address': ip} if ip
            else {'family': 'inet6', 'scope': 'link', 'address': 'fe80::1'}
        ]}}}
        return {'name': name, 'state': state}

    def test_pin_existing_vms_pins_and_syncs(self):
        insts = [self._inst('vm1', 'ip-5'), self._inst('vm2')]
        with (
            mock.patch.object(vm, '_incus_json', return_value=insts),
            mock.patch.object(vm, '_pinned_ips', return_value={}),
            mock.patch.object(vm, '_pin_instance_ip') as pin,
            mock.patch.object(vm, 'sync_dns_records') as sync,
        ):
            changes = vm.pin_existing_vms()
        self.assertEqual(len(changes), 2)
        # vm1 keeps its unique live IP; vm2 gets the pool's first free one.
        self.assertEqual(dict((n, ip) for n, ip, _ in changes),
                         {'vm1': 'ip-5', 'vm2': '10.100.0.10'})
        self.assertEqual(changes[0][2], 'kept current')
        self.assertEqual(changes[1][2], 'was n/a (stopped)')
        sync.assert_called_once()

    def test_pin_existing_vms_reports_collision(self):
        insts = [self._inst('vm1', 'ip-5'), self._inst('vm2', 'ip-5')]
        with (
            mock.patch.object(vm, '_incus_json', return_value=insts),
            mock.patch.object(vm, '_pinned_ips', return_value={}),
            mock.patch.object(vm, '_pin_instance_ip'),
            mock.patch.object(vm, 'sync_dns_records'),
        ):
            changes = vm.pin_existing_vms()
        notes = dict((n, note) for n, _, note in changes)
        self.assertEqual(notes['vm2'], 'was ip-5 (collision)')
