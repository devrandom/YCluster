"""Tests for admin database provisioning.

Follows the app_usage.py pattern: SQL-bearing functions take a cursor, so
tests use plain fake cursors with no DB-driver involvement. The provision()
function takes an injectable connect() seam.
"""

import importlib.util
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

    def __init__(self, fetch_results):
        self.statements = []
        self.fetch_results = fetch_results  # sql fragment -> row or None

    def execute(self, sql, params=None):
        self.statements.append(sql)
        for fragment, row in self.fetch_results.items():
            if fragment in sql:
                self._last = row
                return
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetchone(self):
        return self._last


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
    def _connect(self, role_exists, database_exists):
        """Build a connect() fake; returns (connect, statements)."""
        statements = []

        def connect(dbname):
            fetch_results = {
                'pg_roles': (1,) if role_exists else None,
                'pg_database': (1,) if database_exists else None,
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
        provision.provision(connect, 'pw')
        self.assertEqual([db for db, _, _ in statements],
                         ['postgres', provision.DATABASE])
        schema_cur = statements[1][1]
        self.assertIn(provision.SCHEMA, schema_cur.statements)
        self.assertIn(provision.GRANT_AUDIT_SQL, schema_cur.statements)
        self.assertIn(provision.REVOKE_AUDIT_SQL, schema_cur.statements)

    def test_provision_uses_autocommit_for_role_phase(self):
        # CREATE DATABASE/USER cannot run inside a transaction block.
        connect, statements = self._connect(role_exists=False,
                                            database_exists=False)
        provision.provision(connect, 'pw')
        self.assertTrue(statements[0][2].autocommit)
        self.assertFalse(statements[1][2].autocommit)

    def test_provision_skips_existing_database(self):
        connect, statements = self._connect(role_exists=True,
                                            database_exists=True)
        provision.provision(connect, 'pw')
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
        self.schema = provision.SCHEMA

    def test_truncate_guard_trigger_present(self):
        self.assertIn('BEFORE TRUNCATE ON audit_events', self.schema)

    def test_row_level_immutable_trigger_present(self):
        self.assertIn('BEFORE UPDATE OR DELETE ON audit_events', self.schema)


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


if __name__ == '__main__':
    unittest.main()
