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


if __name__ == '__main__':
    unittest.main()
