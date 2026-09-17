"""Tests for project/quota domain logic and admin routes."""

import unittest
from unittest import mock

import app as appmod
import app_projects
from app_projects import ValidationError


class FakeCursor:
    """Small DB-API fake with just the SQL shapes used by app_projects."""

    def __init__(self, rows=None, member_row=None):
        self.rows = rows if rows is not None else []
        self.member_row = member_row  # SELECT is_default probe result
        self.executed = []
        self._last_fetch = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith('SELECT'):
            sql_key = ' '.join(sql.split()).lower()
            if 'idp_project = any' in sql_key:
                self._last_fetch = [(project,) for project in params[0]
                                    if project == 'p']
            elif 'from projects where id = any' in sql_key:
                self._last_fetch = [(project,) for project in params[0]
                                    if project == 'p']
            elif 'from quota_types where id = any' in sql_key:
                self._last_fetch = [('gpu-hours',)] if 'gpu-hours' in params[0] else []
            elif 'from projects where id' in sql_key:
                self._last_fetch = [(params[0], 'Project', '', '', True,
                                     None, None)] if params[0] == 'p' else []
            elif 'from project_members' in sql_key:
                self._last_fetch = [self.member_row] \
                    if self.member_row else []
            elif 'from quota_types' in sql_key:
                self._last_fetch = [('gpu-hours',)] if params[0] == 'gpu-hours' else []
            elif 'from project_quotas' in sql_key and 'for update' in sql_key:
                self._last_fetch = [(1,)] if params[0] == 'p' else []
            else:
                self._last_fetch = list(self.rows)
        elif 'INSERT INTO project_quotas' in sql:
            self.rows.append(params)
        elif 'INSERT INTO audit_events' in sql:
            self.audit_params = params

    def fetchone(self):
        return self._last_fetch[0] if self._last_fetch else None

    def fetchall(self):
        return list(self._last_fetch)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self, member_row=None):
        self.cursor_obj = FakeCursor(member_row=member_row)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.committed = True
        return False

    def close(self):
        pass


class ProjectCsvTests(unittest.TestCase):
    def test_parse_csv_valid_and_normalizes_project_id(self):
        rows = app_projects.parse_quota_csv(
            'project-id,quota-type,quota-id,value\n'
            'p,gpu-hours,2026-09,10.5\n')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['project_id'], 'p')
        self.assertEqual(str(rows[0]['value']), '10.5')

    def test_parse_csv_header_is_exact(self):
        with self.assertRaisesRegex(ValidationError, 'headers'):
            app_projects.parse_quota_csv('project,quota-type,quota-id,value\n')

    def test_parse_csv_rejects_bad_value_and_duplicate(self):
        bad = 'project-id,quota-type,quota-id,value\np,gpu-hours,x,-1\n'
        with self.assertRaisesRegex(ValidationError, 'row 2'):
            app_projects.parse_quota_csv(bad)

        duplicate = ('project-id,quota-type,quota-id,value\n'
                     'p,gpu-hours,x,1\np,gpu-hours,x,2\n')
        with self.assertRaisesRegex(ValidationError, 'duplicate'):
            app_projects.parse_quota_csv(duplicate)

    def test_import_all_or_nothing_rejects_unknown_project(self):
        conn = FakeConn()
        with conn.cursor() as cur:
            with self.assertRaisesRegex(ValidationError, 'unknown project'):
                app_projects.import_quotas(
                    cur, mock.Mock(uid='u', email='a@x'),
                    'project-id,quota-type,quota-id,value\nbad,gpu-hours,x,1\n')
            quota_writes = [sql for sql, _ in cur.executed
                            if 'INSERT INTO project_quotas' in sql]
            self.assertEqual(quota_writes, [])

    def test_import_upserts_changed_rows_and_records_batch(self):
        conn = FakeConn()
        with conn.cursor() as cur:
            result = app_projects.import_quotas(
                cur, mock.Mock(uid='u', email='a@x'),
                'project-id,quota-type,quota-id,value\n'
                'p,gpu-hours,x,3\n')
            self.assertEqual(result, {
                'rows': 1, 'changed': 1, 'batch_id': result['batch_id']})
            self.assertTrue(any('INSERT INTO project_quotas' in sql
                                for sql, _ in cur.executed))
            self.assertTrue(any('INSERT INTO audit_events' in sql
                                for sql, _ in cur.executed))
            # Internal callers have no email; the audit table rejects NULL.
            self.assertEqual(cur.audit_params[1], 'a@x')
            app_projects.import_quotas(
                cur, mock.Mock(uid='u', email=None),
                'project-id,quota-type,quota-id,value\np,gpu-hours,y,1\n')
            self.assertEqual(cur.audit_params[1], '')


class ProjectDomainTests(unittest.TestCase):
    def test_default_project_is_cleared_for_same_user(self):
        conn = FakeConn()
        with conn.cursor() as cur:
            app_projects.add_member(cur, mock.Mock(uid='u', email='a@x'),
                                    'p', 'user-1', 'a@x', True)
            default_updates = [sql for sql, _ in cur.executed
                               if 'SET is_default = FALSE' in sql]
            self.assertEqual(len(default_updates), 1)

    def test_readding_member_preserves_existing_default(self):
        # Regression: re-adding an existing default member without the
        # checkbox must not demote them (leaving the user with no default).
        conn = FakeConn(member_row=(True,))
        with conn.cursor() as cur:
            app_projects.add_member(cur, mock.Mock(uid='u', email='a@x'),
                                    'p', 'user-1', 'a@x', False)
            upserts = [(sql, params) for sql, params in cur.executed
                       if 'INSERT INTO project_members' in sql]
            self.assertEqual(len(upserts), 1)
            # final_default must be True — preserved from the existing row.
            self.assertEqual(upserts[0][1][3], True)
            self.assertNotIn('SET is_default = FALSE',
                             upserts[0][0])

    def test_set_default_does_not_clobber_assignment_email(self):
        # Regression: promoting an existing member to default used to
        # overwrite email_at_assignment with the operator's email/uid.
        conn = FakeConn(member_row=(True,))
        with conn.cursor() as cur:
            app_projects.set_default_member(
                cur, mock.Mock(uid='admin', email=None),
                'p', 'user-1', email='member@x')
            updates = [(sql, params) for sql, params in cur.executed
                       if 'email_at_assignment' in sql]
            # Existing membership: no email writes at all.
            self.assertEqual(updates, [])
            self.assertTrue(any('SET is_default = TRUE' in sql
                                for sql, _ in cur.executed))

    def test_value_precision_and_negative_validation(self):
        for value in ('-1', 'x', '1.00001', 'nan'):
            with self.assertRaises(ValidationError):
                app_projects.clean_quota_value(value)
        self.assertEqual(str(app_projects.clean_quota_value('1.0000')), '1')


class ManagerScopingTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def _cursor(self):
        return FakeCursor()

    def test_list_projects_for_idp_filters_by_idp_project(self):
        cur = self._cursor()
        app_projects.list_projects_for_idp(cur, {'acme', 'billing'})
        sql, params = cur.executed[-1]
        self.assertIn('idp_project = ANY', sql)
        self.assertEqual(params[0], ['acme', 'billing'])

    def test_list_projects_for_idp_empty_is_no_query(self):
        cur = self._cursor()
        self.assertEqual(app_projects.list_projects_for_idp(cur, set()), [])
        self.assertEqual(cur.executed, [])

    def test_route_scopes_projects_for_manager(self):
        conn = FakeConn()
        headers = {'X-Authentik-UID': 'uid-mgr',
                   'X-Authentik-Email': 'mgr@example.com',
                   'X-Authentik-Groups': 'ycluster-managers'}
        from ycluster.utils import authentik_manager
        with mock.patch('app_project_db.connect', return_value=conn), \
                mock.patch.object(authentik_manager, 'users_data',
                                  lambda: []), \
                mock.patch.object(authentik_manager, 'manager_projects',
                                  lambda email: ['acme']) as mp, \
                mock.patch.object(appmod.app_projects,
                                  'list_projects_for_idp') as scoped:
            scoped.return_value = []
            self.http.get('/admin/projects/data', headers=headers)
        scoped.assert_called_once()
        self.assertEqual(scoped.call_args.args[1], {'acme'})

    def test_route_manager_with_no_projects_sees_nothing(self):
        conn = FakeConn()
        headers = {'X-Authentik-UID': 'uid-mgr',
                   'X-Authentik-Email': 'mgr@example.com',
                   'X-Authentik-Groups': 'ycluster-managers'}
        from ycluster.utils import authentik_manager
        with mock.patch('app_project_db.connect', return_value=conn), \
                mock.patch.object(authentik_manager, 'users_data',
                                  lambda: []), \
                mock.patch.object(authentik_manager, 'manager_projects',
                                  lambda email: []):
            response = self.http.get('/admin/projects/data', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['projects'], [])


class ProjectRoutesTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_project_routes_are_admin_only(self):
        headers = {'X-Authentik-UID': 'uid-user',
                   'X-Authentik-Email': 'user@example.com'}
        self.assertEqual(self.http.get('/admin/projects',
                                      headers=headers).status_code, 403)
        self.assertEqual(self.http.get('/admin/projects/data',
                                       headers=headers).status_code, 403)
        self.assertEqual(self.http.post('/admin/projects/create',
                                        headers=headers, json={}).status_code,
                         403)

    def test_data_endpoint_scopes_users_for_managers(self):
        # A manager must only see users in their authorized IdP projects.
        roster = [
            {'uid': '1', 'username': 'in-scope', 'email': 'in@x',
             'name': 'In', 'project_name': 'acme'},
            {'uid': '2', 'username': 'out-scope', 'email': 'out@x',
             'name': 'Out', 'project_name': 'other'},
        ]
        conn = FakeConn()
        seen = {}

        def fake_users_data():
            seen['called'] = True
            return roster

        headers = {'X-Authentik-UID': 'uid-mgr',
                   'X-Authentik-Email': 'mgr@example.com',
                   'X-Authentik-Groups': 'ycluster-managers'}
        from ycluster.utils import authentik_manager
        with mock.patch('app_project_db.connect', return_value=FakeConn()), \
                mock.patch.object(authentik_manager, 'users_data',
                                  fake_users_data), \
                mock.patch.object(authentik_manager, 'manager_projects',
                                  lambda email: ['acme']):
            response = self.http.get('/admin/projects/data', headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['is_admin'], False)
        self.assertEqual([u['uid'] for u in data['users']], ['1'])
        self.assertTrue(seen.get('called'))

    def test_data_endpoint_admin_sees_all_users(self):
        roster = [
            {'uid': '1', 'username': 'in-scope', 'email': 'in@x',
             'name': 'In', 'project_name': 'acme'},
            {'uid': '2', 'username': 'out-scope', 'email': 'out@x',
             'name': 'Out', 'project_name': 'other'},
        ]
        conn = FakeConn()
        headers = {'X-Authentik-UID': 'uid-admin',
                   'X-Authentik-Email': 'admin@example.com',
                   'X-Authentik-Groups': 'ycluster-admins'}
        from ycluster.utils import authentik_manager
        with mock.patch('app_project_db.connect', return_value=conn), \
                mock.patch.object(authentik_manager, 'users_data',
                                  lambda: roster):
            response = self.http.get('/admin/projects/data', headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['is_admin'], True)
        self.assertEqual(len(data['users']), 2)

    def test_create_project_uses_transaction(self):
        conn = FakeConn()
        operation = mock.Mock(return_value={'id': 'p'})
        with mock.patch('app_project_db.connect', return_value=conn), \
                mock.patch('secrets.token_urlsafe', return_value='req'), \
                mock.patch.object(appmod.app_projects, 'create_project',
                                  operation):
            response = self.http.post(
                '/admin/projects/create', json={'id': 'p', 'name': 'P'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(conn.committed)
        operation.assert_called_once()
        actor_sql, actor_params = conn.cursor_obj.executed[1]
        actor_sql2, actor_params2 = conn.cursor_obj.executed[2]
        self.assertIn('ycluster.actor_uid', actor_sql)
        self.assertEqual(actor_params[0], 'internal')
        self.assertIn('ycluster.actor_email', actor_sql2)
        self.assertEqual(actor_params2[0], '')

    def test_import_reports_validation_error(self):
        conn = FakeConn()
        with mock.patch('app_project_db.connect', return_value=conn):
            response = self.http.post('/admin/projects/import', json={'csv': ''})
        self.assertEqual(response.status_code, 400)
        self.assertIn('CSV headers', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
