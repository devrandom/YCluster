import unittest
from unittest import mock

import app_usage
from ycluster.utils import authentik_manager


class FetchUsageStateTests(unittest.TestCase):
    def test_fetches_events_samples_and_states(self):
        cur = mock.Mock()
        cur.fetchall.side_effect = [
            [('pre',)], [('in',)], [('vm', 'owner', 2.0, 3)], [('vm', 'Running')]
        ]
        events, sampled, states = app_usage.fetch_usage_state(cur, 'since')
        self.assertEqual(events, [('pre',), ('in',)])
        self.assertEqual(sampled, {'vm': {'owner': 'owner', 'observed': 2.0, 'samples': 3}})
        self.assertEqual(states, {'vm': 'Running'})
        self.assertEqual(cur.execute.call_count, 4)


class RegistrySnapshotsTests(unittest.TestCase):
    def test_best_effort_registry(self):
        vmm = mock.Mock()
        vmm.vms_all.side_effect = RuntimeError
        self.assertEqual(app_usage.registry_snapshots(vmm), ({}, {}))
        vmm.vms_all.side_effect = None
        vmm.vms_all.return_value = {}
        vmm.desired_all.return_value = {'vm': {}}
        self.assertEqual(app_usage.registry_snapshots(vmm), ({}, {'vm': {}}))


class GpuTypeTests(unittest.TestCase):
    def test_nvidia_marketing_name_normalized(self):
        self.assertEqual(
            app_usage.gpu_type_label({
                'vendor': 'NVIDIA',
                'model': 'NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition',
            }), 'RTX PRO 6000')

    def test_nvidia_generic_pci_id_from_old_lspci_db(self):
        self.assertEqual(
            app_usage.gpu_type_label({'vendor': 'NVIDIA Corporation',
                                      'model': 'Device 2bb5'}),
            'RTX PRO 6000')

    def test_strix_halo_uses_cpu_family(self):
        self.assertEqual(
            app_usage.gpu_type_label({
                'vendor': 'Advanced Micro Devices, Inc. [AMD/ATI]',
                'model': 'Device 1586',
            }, cpu='AMD RYZEN AI MAX+ 395 w/ Radeon 8060S, 16c/32t'),
            'AMD 395+')

    def test_strix_halo_without_cpu_is_generic(self):
        self.assertEqual(
            app_usage.gpu_type_label({'vendor': 'AMD', 'model': 'Strix Halo'}),
            'AMD Strix Halo')

    def test_bmc_display_chip_is_not_a_gpu(self):
        self.assertEqual(
            app_usage.gpu_type_label({'vendor': 'ASPEED Technology, Inc.',
                                      'model': 'ASPEED Graphics Family'}), '')

    def test_other_names_pass_through(self):
        self.assertEqual(
            app_usage.gpu_type_label({'vendor': 'NVIDIA',
                                      'model': 'NVIDIA RTX A6000'}),
            'RTX A6000')
        self.assertEqual(
            app_usage.gpu_type_label({'vendor': 'Apple',
                                      'model': 'Apple M3 Ultra'}),
            'Apple M3 Ultra')


class GpuTypesByHostTests(unittest.TestCase):
    def test_dedupes_repeated_cards_and_survives_lookup_failure(self):
        registry = {
            'vm-a': {'host': 'gpu-a'},
            'vm-b': {'host': 'gpu-a'},
            'vm-c': {'host': 'gone'},
            'vm-d': {},
        }
        hw = {'gpu-a': {'cpu': 'Xeon', 'gpus': [
            {'vendor': 'NVIDIA Corporation', 'model': 'Device 2bb5'},
            {'vendor': 'NVIDIA Corporation', 'model': 'Device 2bb5'},
        ]}}

        def get_hardware(host):
            if host == 'gone':
                raise RuntimeError('etcd down')
            return hw[host]

        self.assertEqual(app_usage.gpu_types_by_host(registry, get_hardware),
                         {'gpu-a': 'RTX PRO 6000', 'gone': ''})


class OwnerVisibilityTests(unittest.TestCase):
    def test_non_manager_visible(self):
        identity = mock.Mock(is_manager=False, is_admin=False)
        self.assertTrue(app_usage.owner_visibility(identity, mock.Mock())('owner'))

    def test_manager_project_cache(self):
        manager = mock.Mock()
        manager.manager_projects.return_value = ['proj']
        manager.user_project.side_effect = ['proj', 'other']
        identity = mock.Mock(is_manager=True, is_admin=False, email='owner-user')
        with mock.patch.object(authentik_manager, 'manager_projects',
                        manager.manager_projects), \
             mock.patch.object(authentik_manager, 'user_project',
                        manager.user_project):
            visible = app_usage.owner_visibility(identity, manager)
            self.assertTrue(visible('owner'))
            self.assertFalse(visible('other'))
        self.assertEqual(manager.user_project.call_count, 2)

    def test_manager_owner_lookup_fails_closed(self):
        manager = mock.Mock()
        manager.manager_projects.return_value = ['proj']
        manager.user_project.side_effect = RuntimeError('down')
        identity = mock.Mock(is_manager=True, is_admin=False, email='owner-user')
        with mock.patch.object(authentik_manager, 'manager_projects',
                        manager.manager_projects), \
             mock.patch.object(authentik_manager, 'user_project',
                        manager.user_project):
            visible = app_usage.owner_visibility(identity, manager)
        self.assertFalse(visible('owner'))
        manager.user_project.assert_called_once_with('owner')

    def test_manager_empty_email(self):
        manager = mock.Mock()
        manager.manager_projects.return_value = []
        identity = mock.Mock(is_manager=True, is_admin=False, email='')
        with mock.patch.object(authentik_manager, 'manager_projects',
                        manager.manager_projects):
            visible = app_usage.owner_visibility(identity, manager)
        self.assertFalse(visible('owner'))
        manager.manager_projects.assert_called_once_with('')



class UsageRowsTests(unittest.TestCase):
    def test_filters_and_sorts(self):
        row_fn = mock.Mock(side_effect=lambda vm, *_: vm)
        visible = mock.Mock(side_effect=lambda owner: owner == 'ok')
        got = app_usage.usage_rows(
            {'b': {'owner': 'ok'}, 'a': {'owner': 'hidden'}}, {},
            {}, {}, {}, visible, row_fn)
        self.assertEqual(got, ['b'])
        row_fn.assert_called_once()

    def test_attaches_gpu_type_from_host(self):
        gpus = {'vm1': 2, 'vm2': 1, 'vm3': 0}

        def row_fn(vm, *_):
            return {'vm': vm, 'gpus': gpus[vm]}

        got = app_usage.usage_rows(
            {'vm1': {}, 'vm2': {}, 'vm3': {}}, {},
            {'vm1': {'host': 'gpu-a'}, 'vm2': {'host': 'gpu-b'},
             'vm3': {'host': 'gpu-a'}},
            {}, {}, lambda owner: True, row_fn,
            gpu_types={'gpu-a': 'RTX PRO 6000', 'gpu-b': 'AMD 395+'})
        self.assertEqual(got, [
            {'vm': 'vm1', 'gpus': 2, 'gpu_type': 'RTX PRO 6000'},
            {'vm': 'vm2', 'gpus': 1, 'gpu_type': 'AMD 395+'},
            {'vm': 'vm3', 'gpus': 0, 'gpu_type': ''},
        ])


class UsageCsvTests(unittest.TestCase):
    def test_header_and_row(self):
        import csv
        buf = app_usage.usage_csv([{
            'vm': 'vm1', 'owner': 'a@x', 'gpus': 2,
            'gpu_type': 'RTX PRO 6000', 'current': 'untracked',
            'schedule_mode': 'schedule',
            'windows': [{'start': '2026-01-01T00:00:00+00:00',
                         'end': '2026-01-02T00:00:00+00:00'}],
            'billable_gpu_hours': 4.0, 'tracked_gpu_hours': 3.0,
            'observed_gpu_hours': 5.5, 'samples': 42,
        }])
        rows = list(csv.DictReader(buf))
        self.assertEqual(rows[0]['VM'], 'vm1')
        self.assertEqual(rows[0]['GPU type'], 'RTX PRO 6000')
        self.assertEqual(rows[0]['Now'], 'running (untracked)')
        self.assertEqual(rows[0]['Delta GPU-h'], '2.50')
        self.assertIn('2026-01-01T00:00:00+00:00 -> ', rows[0]['Windows'])


if __name__ == '__main__':
    unittest.main()
