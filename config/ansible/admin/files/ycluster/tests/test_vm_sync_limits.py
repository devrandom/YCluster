import unittest
from unittest import mock

from ycluster.utils import vm_manager


class VMSyncLimitsTest(unittest.TestCase):
    def test_already_backfilled_is_noop(self):
        rec = {'host': 'h', 'cpu': '4', 'mem': '8GiB'}
        with mock.patch.object(vm_manager, '_incus') as incus:
            self.assertFalse(vm_manager._sync_vm_limits_once('vm1', rec))
        incus.assert_not_called()

    def test_backfills_cpu_mem(self):
        rec = {'host': 'h'}
        calls = [mock.Mock(returncode=0, stdout='4\n'), mock.Mock(returncode=0, stdout='8GiB\n')]
        with mock.patch.object(vm_manager, '_incus', side_effect=calls) as incus, \
             mock.patch.object(vm_manager, '_put_json') as put:
            self.assertTrue(vm_manager._sync_vm_limits_once('vm1', rec))
        self.assertEqual(rec['cpu'], '4')
        self.assertEqual(rec['mem'], '8GiB')
        put.assert_called_once()


if __name__ == '__main__':
    unittest.main()
