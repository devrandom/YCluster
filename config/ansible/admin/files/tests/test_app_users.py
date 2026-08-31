"""Unit tests for the pure helpers behind the admin user actions."""

import unittest

import app_users


class AdminUserActionTests(unittest.TestCase):
    def _manager(self):
        class Manager:
            def __init__(self):
                self.calls = []

            def revoke_invitation(self, email):
                self.calls.append(('revoke', email))
                return 2

            def set_admin(self, email, remove):
                self.calls.append(('admin', email, remove))
                return 'admin'

            def set_manager(self, email, remove):
                self.calls.append(('manager', email, remove))
                return 'manager'

            def set_active(self, email, active):
                self.calls.append(('active', email, active))
                return 'active'

        return Manager()

    def test_uninvite_reports_count(self):
        manager = self._manager()
        result = app_users.admin_user_action('uninvite', 'actor-one', manager)
        self.assertEqual(result, ({'ok': True, 'message': 'revoked 2 invitation(s)'}, None))
        self.assertEqual(manager.calls, [('revoke', 'actor-one')])

    def test_admin_and_manager_role_changes(self):
        manager = self._manager()
        self.assertEqual(
            app_users.admin_user_action('admin_add', 'actor-one', manager),
            ({'ok': True, 'message': 'admin'}, None))
        self.assertEqual(
            app_users.admin_user_action('admin_remove', 'actor-one', manager),
            ({'ok': True, 'message': 'admin'}, None))
        self.assertEqual(
            app_users.admin_user_action('manager_add', 'actor-one', manager),
            ({'ok': True, 'message': 'manager'}, None))
        self.assertEqual(
            app_users.admin_user_action('manager_remove', 'actor-one', manager),
            ({'ok': True, 'message': 'manager'}, None))
        self.assertEqual(manager.calls[0], ('admin', 'actor-one', False))
        self.assertEqual(manager.calls[1], ('admin', 'actor-one', True))
        self.assertEqual(manager.calls[2], ('manager', 'actor-one', False))
        self.assertEqual(manager.calls[3], ('manager', 'actor-one', True))

    def test_activation_changes(self):
        manager = self._manager()
        self.assertEqual(
            app_users.admin_user_action('activate', 'actor-one', manager),
            ({'ok': True, 'message': 'active'}, None))
        self.assertEqual(
            app_users.admin_user_action('deactivate', 'actor-one', manager),
            ({'ok': True, 'message': 'active'}, None))
        self.assertEqual(manager.calls, [
            ('active', 'actor-one', True), ('active', 'actor-one', False)])

    def test_unknown_action_is_rejected(self):
        result = app_users.admin_user_action('bogus', 'actor-one', self._manager())
        self.assertEqual(result, ({'error': 'unknown action'}, 400))


if __name__ == '__main__':
    unittest.main()
