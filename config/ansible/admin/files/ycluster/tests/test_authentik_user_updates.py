"""Tests for mutable Authentik user fields."""

import unittest
from unittest import mock

from ycluster.utils import authentik_manager


class UpdateUserTests(unittest.TestCase):
    def update(self, attributes, **kwargs):
        api = mock.Mock()
        with mock.patch.object(authentik_manager, 'AuthentikAPI',
                               return_value=api), \
                mock.patch.object(authentik_manager, '_get_user',
                                  return_value={'pk': 7, 'attributes': attributes}):
            result = authentik_manager.update_user('actor', **kwargs)
        return result, api

    def test_name_only_skips_attributes(self):
        result, api = self.update({}, name='New Name')
        self.assertEqual(result, 'updated actor')
        api.patch.assert_called_once_with(
            '/core/users/7/', json={'name': 'New Name'})

    def test_project_only_preserves_other_attributes(self):
        attrs = {'admin_notes': 'keep'}
        _, api = self.update(attrs, project_name='demo')
        api.patch.assert_called_once_with(
            '/core/users/7/',
            json={'attributes': {'admin_notes': 'keep', 'project_name': 'demo'}})

    def test_notes_only_reads_missing_attributes(self):
        _, api = self.update(None, admin_notes='note')
        api.patch.assert_called_once_with(
            '/core/users/7/', json={'attributes': {'admin_notes': 'note'}})

    def test_no_updates_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'no updates specified'):
            self.update({'project_name': 'demo'})


if __name__ == '__main__':
    unittest.main()
