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
        conn.cursor.return_value.__enter__.return_value = cur
        conn.__enter__.return_value = conn
        return conn, cur

    def test_migrate_creates_project_per_user(self):
        conn, cur = self._mock_conn()
        users = [
            {'uid': '1', 'username': 'alice@x.org', 'email': 'alice@x.org'},
            {'uid': '2', 'username': 'bob@x.org', 'email': 'bob@x.org'},
        ]
        count = migration.migrate(conn, users)

        self.assertEqual(count, 2)
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertEqual(
            len([s for s in sqls if 'INSERT INTO projects' in s]), 2)
        self.assertEqual(
            len([s for s in sqls if 'INSERT INTO project_members' in s]), 2)
        # All user data goes through parameterized queries, never f-string
        # interpolation into SQL.
        params = [tuple(call.args[1]) for call in cur.execute.call_args_list
                  if len(call.args) > 1 and call.args[1]]
        self.assertIn(('alice-x-org', 'alice@x.org'), params)
        self.assertIn(('bob-x-org', 'bob@x.org'), params)

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
        self.assertIn(('o-brien', "o'brien", "o'brien@x.org"), bound)

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
        count = migration.migrate(
            conn, [{'uid': '1', 'username': '', 'email': 'a@x'}])
        self.assertEqual(count, 0)
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
