"""Tests for the project-defaults migration script."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'migrate_project_defaults', _ROOT / 'migrate-project-defaults.py')
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


class SlugifyTests(unittest.TestCase):
    def test_slugify_handles_usernames(self):
        self.assertEqual(migration.slugify('alice@x.org'), 'alice-x-org')
        self.assertEqual(migration.slugify('Bob_Smith'), 'bob-smith')
        self.assertEqual(migration.slugify('a.b+tag@x.org'), 'a-b-tag-x-org')

    def test_slugify_rejects_unusable_names(self):
        with self.assertRaises(Exception):
            migration.slugify('   ')
        with self.assertRaises(Exception):
            migration.slugify('---')


class MigrateTests(unittest.TestCase):
    def _mock_conn(self):
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        cur.rowcount = 1
        cur.fetchone.return_value = None  # no existing default membership
        conn.cursor.return_value.__enter__.return_value = cur
        conn.__enter__.return_value = conn
        return conn, cur

    def test_migrate_creates_project_per_user(self):
        conn, cur = self._mock_conn()
        users = [
            {'uid': '1', 'username': 'alice@x.org', 'email': 'alice@x.org',
             'project_name': 'Acme'},
            {'uid': '2', 'username': 'bob@x.org', 'email': 'bob@x.org',
             'project_name': ''},
        ]
        processed, changed = migration.migrate(conn, users)

        self.assertEqual((processed, changed), (2, 2))
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertEqual(
            len([s for s in sqls if 'INSERT INTO projects' in s]), 2)
        self.assertEqual(
            len([s for s in sqls if 'INSERT INTO project_members' in s]), 2)
        # All user data goes through parameterized queries, never f-string
        # interpolation into SQL.
        params = [tuple(call.args[1]) for call in cur.execute.call_args_list
                  if len(call.args) > 1 and call.args[1]]
        self.assertIn(('alice-x-org', 'alice@x.org', 'Acme'), params)
        self.assertIn(('bob-x-org', 'bob@x.org', ''), params)
        project_sql = next(s for s in sqls if 'INSERT INTO projects' in s)
        self.assertIn('ON CONFLICT (id) DO NOTHING', project_sql)
        update_sql = next(
            s for s in sqls if 'UPDATE projects' in s
            and 'SET idp_project' in s)
        self.assertIn("idp_project = '' AND %s <> ''", update_sql)
        self.assertIn('updated_at = now()', update_sql)

    def test_idp_project_uses_first_list_item(self):
        self.assertEqual(
            migration.idp_project({'project_name': ['Acme', 'Billing']}),
            'Acme')
        self.assertEqual(
            migration.idp_project({'project_name': ['', 'Billing']}),
            'Billing')
        self.assertEqual(
            migration.idp_project({'project_name': ['  ', 'Billing']}),
            'Billing')
        self.assertEqual(migration.idp_project({}), '')

    def test_migrate_user_data_is_never_interpolated(self):
        # A username with a single quote must reach the DB as a bound
        # parameter, not break or inject SQL.
        conn, cur = self._mock_conn()
        users = [{'uid': "o'brien", 'username': "o'brien",
                  'email': "o'brien@x.org"}]
        migration.migrate(conn, users)
        joined_sql = ' '.join(call.args[0] for call in
                              cur.execute.call_args_list)
        self.assertNotIn("o''brien", joined_sql)
        self.assertNotIn("'o'brien", joined_sql)
        bound = [call.args[1] for call in cur.execute.call_args_list
                 if len(call.args) > 1 and call.args[1]]
        self.assertIn(('o-brien', "o'brien", ''), bound)

    def test_migrate_skips_membership_writes_when_default_is_correct(self):
        conn, cur = self._mock_conn()
        cur.fetchone.return_value = ('alice-x-org',)
        processed, changed = migration.migrate(
            conn, [{'uid': '1', 'username': 'alice@x.org',
                    'email': 'alice@x.org', 'project_name': 'Acme'}])

        self.assertEqual((processed, changed), (1, 0))
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any('INSERT INTO projects' in s for s in sqls))
        self.assertFalse(any('UPDATE project_members' in s for s in sqls))
        self.assertFalse(any('INSERT INTO project_members' in s for s in sqls))

    def test_migrate_preserves_admin_reassigned_default(self):
        conn, cur = self._mock_conn()
        cur.fetchone.return_value = ('other-project',)
        processed, changed = migration.migrate(
            conn, [{'uid': '1', 'username': 'alice@x.org',
                    'email': 'alice@x.org'}])

        self.assertEqual((processed, changed), (1, 0))
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any('INSERT INTO projects' in s for s in sqls))
        self.assertFalse(any('UPDATE project_members' in s for s in sqls))
        self.assertFalse(any('INSERT INTO project_members' in s for s in sqls))

    def test_migrate_rejects_slug_collisions(self):
        conn, _ = self._mock_conn()
        users = [
            {'uid': '1', 'username': 'a.b@x', 'email': 'a@x'},
            {'uid': '2', 'username': 'a_b@x', 'email': 'b@x'},
        ]
        with self.assertRaisesRegex(Exception, 'both map to'):
            migration.migrate(conn, users)

    def test_migrate_skips_users_without_username(self):
        conn, cur = self._mock_conn()
        processed, changed = migration.migrate(
            conn, [{'uid': '1', 'username': '', 'email': 'a@x'}])
        self.assertEqual((processed, changed), (0, 0))
        inserts = [call for call in cur.execute.call_args_list
                   if 'INSERT INTO' in call.args[0]]
        self.assertEqual(inserts, [])


class CliModeTests(unittest.TestCase):
    def test_apply_reads_stdin_json(self):
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.__enter__.return_value = conn
        users = [{'uid': '1', 'username': 'alice@x.org',
                  'email': 'alice@x.org'}]
        with mock.patch.object(migration, 'db_connect',
                               return_value=conn), \
                mock.patch.object(migration.sys, 'stdin',
                                  mock.Mock(read=lambda: json.dumps(users))), \
                mock.patch.object(migration.sys, 'argv',
                                  ['x', '--apply']):
            with mock.patch('builtins.print'):
                migration.main()
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertTrue(any('INSERT INTO projects' in s for s in sqls))


if __name__ == '__main__':
    unittest.main()
