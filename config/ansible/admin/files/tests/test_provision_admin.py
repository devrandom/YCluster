"""Tests for admin database provisioning.

Follows the app_usage.py pattern: SQL-bearing functions take a cursor, so
tests use plain fake cursors with no DB-driver involvement. The provision()
function takes an injectable connect() seam.
"""

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'provision_admin', _ROOT / 'provision-admin.py')
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)


class FakeCursor:
    """DB-API cursor fake recording executed statements."""

    def __init__(self, fetch_results=None):
        self.statements = []
        self.calls = []
        self.fetch_results = fetch_results or {}
        self._last = None
        self._rows = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.calls.append((sql, params))
        for fragment, rows in self.fetch_results.items():
            if fragment in sql:
                if isinstance(rows, list):
                    self._rows = rows
                    self._last = rows[0] if rows else None
                else:
                    self._rows = [] if rows is None else [rows]
                    self._last = rows
                return
        self._rows = []
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.autocommit = False

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.committed = True
        return False

    def close(self):
        pass


class CreateRoleTests(unittest.TestCase):
    def test_creates_role_when_missing_and_sets_password(self):
        cur = FakeCursor({'pg_roles': None})
        provision.create_role(cur, 'pw')
        self.assertIn(provision.CREATE_ROLE_SQL, cur.statements)
        self.assertIn(provision.ALTER_ROLE_SQL, cur.statements)

    def test_skips_create_when_role_exists(self):
        cur = FakeCursor({'pg_roles': (1,)})
        provision.create_role(cur, 'pw')
        self.assertNotIn(provision.CREATE_ROLE_SQL, cur.statements)
        self.assertIn(provision.ALTER_ROLE_SQL, cur.statements)

    def test_password_is_bound_not_interpolated(self):
        cur = FakeCursor({'pg_roles': None})
        provision.create_role(cur, "pa'ss")
        create_stmt = next(s for s in cur.statements
                           if s == provision.CREATE_ROLE_SQL)
        self.assertNotIn("pa'ss", create_stmt)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.migrations = tempfile.TemporaryDirectory()
        Path(self.migrations.name, '0001_test.sql').write_text(
            'CREATE TABLE test (id int);')

    def tearDown(self):
        self.migrations.cleanup()

    def _connect(self, role_exists, database_exists):
        """Build a connect() fake; returns (connect, statements)."""
        statements = []

        def connect(dbname):
            fetch_results = {
                'pg_roles': (1,) if role_exists else None,
                'pg_database': (1,) if database_exists else None,
                'FROM schema_migrations': [],
            }
            cur = FakeCursor(fetch_results)
            conn = FakeConn(cur)
            conn.autocommit = False
            statements.append((dbname, cur, conn))
            return conn

        return connect, statements

    def test_provision_creates_role_database_and_grants(self):
        connect, statements = self._connect(role_exists=False,
                                            database_exists=False)
        provision.provision(connect, 'pw', self.migrations.name)
        self.assertEqual([db for db, _, _ in statements],
                         ['postgres', provision.DATABASE])
        schema_cur = statements[1][1]
        self.assertIn('CREATE TABLE test (id int);',
                      schema_cur.statements)
        self.assertIn(provision.GRANT_AUDIT_SQL, schema_cur.statements)
        self.assertIn(provision.REVOKE_AUDIT_SQL, schema_cur.statements)
        self.assertIn(provision.REVOKE_MIGRATIONS_SQL,
                      schema_cur.statements)

    def test_provision_uses_autocommit_for_role_phase(self):
        # CREATE DATABASE/USER cannot run inside a transaction block.
        connect, statements = self._connect(role_exists=False,
                                            database_exists=False)
        provision.provision(connect, 'pw', self.migrations.name)
        self.assertTrue(statements[0][2].autocommit)
        self.assertFalse(statements[1][2].autocommit)

    def test_provision_skips_existing_database(self):
        connect, statements = self._connect(role_exists=True,
                                            database_exists=True)
        provision.provision(connect, 'pw', self.migrations.name)
        create_db = [s for _, cur, _ in statements
                     for s in cur.statements
                     if provision.CREATE_DATABASE_SQL in s]
        self.assertEqual(create_db, [])

    def test_audit_grants_are_least_privilege(self):
        # The app role must not be able to modify or clear audit history.
        self.assertIn('UPDATE, DELETE, TRUNCATE', provision.REVOKE_AUDIT_SQL)
        self.assertEqual(provision.GRANT_AUDIT_SQL,
                         f'GRANT INSERT, SELECT ON audit_events '
                         f'TO {provision.ROLE}')

    def test_migration_table_is_operator_only(self):
        self.assertEqual(
            provision.REVOKE_MIGRATIONS_SQL,
            f'REVOKE ALL PRIVILEGES ON schema_migrations '
            f'FROM {provision.ROLE}')

    def test_app_role_does_not_own_the_database(self):
        # If the role owned the DB it could drop its own audit machinery.
        self.assertNotIn('OWNER', provision.CREATE_DATABASE_SQL)
        self.assertIn('CREATE DATABASE ' + provision.DATABASE,
                      provision.CREATE_DATABASE_SQL)

    def test_no_hba_logic_in_script(self):
        # pg_hba.conf is root-owned; the playbook manages it via blockinfile.
        self.assertFalse(hasattr(provision, 'configure_hba'))


class SchemaContentTests(unittest.TestCase):
    def setUp(self):
        self.schema = (_ROOT / 'migrations' / '0001_baseline.sql').read_text()

    def test_truncate_guard_trigger_present(self):
        self.assertIn('BEFORE TRUNCATE ON audit_events', self.schema)

    def test_row_level_immutable_trigger_present(self):
        self.assertIn('BEFORE UPDATE OR DELETE ON audit_events', self.schema)

    def test_baseline_is_idempotent(self):
        self.assertIn('CREATE TABLE IF NOT EXISTS projects', self.schema)
        self.assertIn('ADD COLUMN IF NOT EXISTS idp_project', self.schema)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, sql):
        (self.directory / name).write_text(sql)

    def _conn(self, applied=()):
        cur = FakeCursor({'FROM schema_migrations': list(applied)})
        return FakeConn(cur), cur

    def test_discovers_in_version_order(self):
        self._write('0002_second.sql', 'SELECT 2;')
        self._write('0001_first.sql', 'SELECT 1;')
        versions = [m['version'] for m in
                    provision.discover_migrations(str(self.directory))]
        self.assertEqual(versions, ['0001', '0002'])

    def test_rejects_invalid_filename(self):
        self._write('1_bad.sql', 'SELECT 1;')
        with self.assertRaisesRegex(RuntimeError, 'invalid migration'):
            provision.discover_migrations(str(self.directory))

    def test_rejects_duplicate_versions(self):
        self._write('0001_a.sql', 'SELECT 1;')
        self._write('0001_b.sql', 'SELECT 2;')
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            provision.discover_migrations(str(self.directory))

    def test_applies_pending_and_records_checksum(self):
        self._write('0001_first.sql', 'SELECT 1;')
        conn, cur = self._conn()
        provision.apply_migrations(conn, str(self.directory))
        checksum = hashlib.sha256(b'SELECT 1;').hexdigest()
        self.assertIn('SELECT 1;', cur.statements)
        self.assertIn(
            ('0001', '0001_first.sql', checksum),
            [call[1] for call in cur.calls
             if call[0].startswith('INSERT INTO schema_migrations')])

    def test_skips_applied_matching_checksum(self):
        self._write('0001_first.sql', 'SELECT 1;')
        checksum = hashlib.sha256(b'SELECT 1;').hexdigest()
        conn, cur = self._conn([('0001', checksum)])
        provision.apply_migrations(conn, str(self.directory))
        self.assertNotIn('SELECT 1;', cur.statements)

    def test_rejects_changed_checksum(self):
        self._write('0001_first.sql', 'SELECT 1;')
        conn, cur = self._conn([('0001', 'not-the-checksum')])
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            provision.apply_migrations(conn, str(self.directory))
        self.assertTrue(any('pg_advisory_unlock' in s
                            for s in cur.statements))

    def test_rejects_database_migration_missing_on_disk(self):
        self._write('0001_first.sql', 'SELECT 1;')
        conn, _ = self._conn([('0002', 'checksum')])
        with self.assertRaisesRegex(RuntimeError, 'not present on disk'):
            provision.apply_migrations(conn, str(self.directory))

    def test_rejects_gap_in_applied_migrations(self):
        self._write('0001_first.sql', 'SELECT 1;')
        self._write('0002_second.sql', 'SELECT 2;')
        conn, _ = self._conn([('0002', 'checksum')])
        with self.assertRaisesRegex(RuntimeError, 'not a prefix'):
            provision.apply_migrations(conn, str(self.directory))


class PasswordTests(unittest.TestCase):
    def test_password_comes_from_environment(self):
        with mock.patch.dict('os.environ',
                             {'ADMIN_DB_PASSWORD': 'secret'}):
            self.assertEqual(provision.get_password(), 'secret')

    def test_missing_password_is_an_error(self):
        with mock.patch.dict('os.environ',
                             {}, clear=True):
            with self.assertRaisesRegex(Exception, 'environment'):
                provision.get_password()


class ConnectTests(unittest.TestCase):
    def test_connect_uses_peer_auth(self):
        import sys
        import types
        fake = types.ModuleType('psycopg2')
        fake.connect = mock.Mock(return_value='conn')
        with mock.patch.dict(sys.modules, {'psycopg2': fake}):
            self.assertEqual(provision.connect('ycluster'), 'conn')
        fake.connect.assert_called_once_with(dbname='ycluster',
                                             user='postgres')


class MigrationDiscoveryTests(unittest.TestCase):
    def test_missing_directory_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            provision.discover_migrations('/nonexistent/migrations')

    def test_ignores_non_sql_files(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'README.md').write_text('notes')
            Path(directory, '0001_first.sql').write_text('SELECT 1;')
            migrations = provision.discover_migrations(directory)
        self.assertEqual([m['name'] for m in migrations], ['0001_first.sql'])


class ReleaseLockTests(unittest.TestCase):
    def test_unlock_failure_is_logged_not_raised(self):
        conn = mock.MagicMock()

        def execute(sql, params=None):
            if "pg_advisory_unlock" in sql:
                raise RuntimeError("gone")

        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = execute
        conn.__enter__.return_value = conn
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '0001_first.sql').write_text('SELECT 1;')
            with mock.patch.object(provision.LOG, 'exception') as log:
                provision.apply_migrations(conn, directory)
        log.assert_called_once()


class _Cursor:
    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    autocommit = False

    def __init__(self):
        self.closed = False

    def cursor(self):
        return _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True


class EntrypointTests(unittest.TestCase):
    def test_module_entrypoint_provisions(self):
        import runpy
        import sys
        import types
        fake = types.ModuleType('psycopg2')
        fake.connect = mock.Mock(side_effect=lambda *a, **k: _Conn())
        with tempfile.TemporaryDirectory() as migrations, \
                mock.patch.dict(sys.modules, {'psycopg2': fake}), \
                mock.patch.dict('os.environ', {
                    'ADMIN_DB_PASSWORD': 'secret',
                    'ADMIN_MIGRATIONS_DIR': migrations,
                }), \
                mock.patch('builtins.print'):
            Path(migrations, '0001_first.sql').write_text('SELECT 1;')
            runpy.run_path(str(_ROOT / 'provision-admin.py'),
                           run_name='__main__')
        self.assertEqual(fake.connect.call_count, 2)


if __name__ == '__main__':
    unittest.main()
