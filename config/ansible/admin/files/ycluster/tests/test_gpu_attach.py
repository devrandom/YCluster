"""Tests for gpu_attach helpers."""

import unittest
from unittest import mock

from ycluster.utils import gpu_attach as ga
from ycluster.utils import vm_manager as vm


class GPUDetachTests(unittest.TestCase):
    def test_container_returns_immediately(self):
        with mock.patch.object(vm, '_instance_info',
                               return_value={'type': 'container'}):
            ga._ensure_gpus_attached('vm1')  # no-op, no error

    def test_unregistered_returns_immediately(self):
        with mock.patch.object(vm, '_instance_info', return_value=None):
            ga._ensure_gpus_attached('vm1')

    def test_detach_excess(self):
        inst = {'type': 'virtual-machine', 'devices': {
            'gpu0': {'type': 'gpu', 'pci': 'a'},
            'gpu1': {'type': 'gpu', 'pci': 'b'},
        }}
        with mock.patch.object(vm, '_instance_info', return_value=inst), \
             mock.patch.object(vm, 'vm_get', return_value={'gpus': 1}), \
             mock.patch.object(vm, '_incus') as incus:
            ga._ensure_gpus_attached('vm1')
        incus.assert_called_once_with('config', 'device', 'remove',
                                      'vm1', 'gpu0')

    def test_no_change(self):
        inst = {'type': 'virtual-machine', 'devices': {
            'gpu0': {'type': 'gpu', 'pci': 'a'},
        }}
        with mock.patch.object(vm, '_instance_info', return_value=inst), \
             mock.patch.object(vm, 'vm_get', return_value={'gpus': 1}), \
             mock.patch.object(vm, '_incus') as incus:
            ga._ensure_gpus_attached('vm1')
        incus.assert_not_called()


class GPUAttachTests(unittest.TestCase):
    def test_attach_one(self):
        inst = {'type': 'virtual-machine', 'devices': {}}
        with mock.patch.object(vm, '_instance_info', return_value=inst), \
             mock.patch.object(vm, 'vm_get', return_value={'gpus': 1}), \
             mock.patch.object(vm, '_select_free_gpus',
                               return_value=['pci1']), \
             mock.patch.object(vm, '_incus') as incus:
            ga._ensure_gpus_attached('vm1')
        incus.assert_called_once_with('config', 'device', 'add', 'vm1',
                                      'gpu0', 'gpu', 'gputype=physical',
                                      'pci=pci1')

    def test_pool_exhausted(self):
        inst = {'type': 'virtual-machine', 'devices': {}}
        with mock.patch.object(vm, '_instance_info', return_value=inst), \
             mock.patch.object(vm, 'vm_get', return_value={'gpus': 2}), \
             mock.patch.object(vm, '_select_free_gpus',
                               side_effect=ValueError('only 1 free')):
            with self.assertRaisesRegex(RuntimeError, 'needs 2 more'):
                ga._ensure_gpus_attached('vm1')


if __name__ == '__main__':
    unittest.main()
