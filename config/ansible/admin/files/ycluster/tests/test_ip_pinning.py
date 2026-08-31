"""Tests for pin_existing_vms helper functions."""

import unittest

from ycluster.utils import vm_manager as vm


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
