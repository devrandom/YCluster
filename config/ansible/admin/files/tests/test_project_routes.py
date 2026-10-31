"""Route tests for the extracted project admin endpoints."""

import unittest
from io import BytesIO
from unittest import mock

import app as appmod
import app_projects


ADMIN = {
    "X-Authentik-UID": "uid-admin",
    "X-Authentik-Email": "admin@example.test",
    "X-Authentik-Groups": "ycluster-admins",
}
MANAGER = {
    "X-Authentik-UID": "uid-manager",
    "X-Authentik-Email": "manager@example.test",
    "X-Authentik-Groups": "ycluster-managers",
}
USER = {
    "X-Authentik-UID": "uid-user",
    "X-Authentik-Email": "user@example.test",
}


class FakeCursor:
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


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class ProjectRouteGuardTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_mutations_reject_non_managers(self):
        paths = [
            "/admin/projects/create",
            "/admin/projects/update",
            "/admin/projects/member/add",
            "/admin/projects/member/remove",
            "/admin/projects/member/default",
            "/admin/projects/quotas/upsert",
            "/admin/projects/quotas/delete",
            "/admin/projects/import",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.http.post(path, headers=USER, json={})
                self.assertEqual(response.status_code, 403)

    def test_audit_rejects_non_managers(self):
        self.assertEqual(
            self.http.get("/admin/projects/audit", headers=USER).status_code,
            403)

    def test_page_renders_for_manager(self):
        self.assertEqual(
            self.http.get("/admin/projects", headers=MANAGER).status_code, 200)


class ProjectDataTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_admin_sees_all_projects_with_detail(self):
        projects = [{"id": "p", "name": "Project"}]
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "list_projects",
                                  return_value=projects), \
                mock.patch.object(appmod.app_projects, "list_members",
                                  return_value=[]), \
                mock.patch.object(appmod.app_projects, "list_quotas",
                                  return_value=[]), \
                mock.patch("ycluster.utils.authentik_manager.users_data",
                           return_value=[]):
            response = self.http.get("/admin/projects/data", headers=ADMIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["projects"], projects)

    def test_database_failure_reports_503(self):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "list_projects",
                                  side_effect=RuntimeError("down")), \
                mock.patch.object(appmod.app.logger, "exception"):
            response = self.http.get("/admin/projects/data", headers=ADMIN)
        self.assertEqual(response.status_code, 503)

    def test_transaction_failure_reports_503(self):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "set_actor"), \
                mock.patch.object(appmod.app_projects, "create_project",
                                  side_effect=RuntimeError("boom")), \
                mock.patch.object(appmod.app.logger, "exception"):
            response = self.http.post("/admin/projects/create",
                                      headers=ADMIN, json={"id": "p"})
        self.assertEqual(response.status_code, 503)


class ProjectMutationTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def _post(self, path, payload):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "set_actor"):
            return self.http.post(path, headers=ADMIN, json=payload)

    def test_create_returns_project(self):
        created = {"id": "p", "name": "Project"}
        with mock.patch.object(appmod.app_projects, "create_project",
                               return_value=created):
            response = self._post("/admin/projects/create",
                                  {"id": "p", "name": "Project"})
        self.assertEqual(response.get_json(), created)

    def test_validation_error_returns_400(self):
        with mock.patch.object(appmod.app_projects, "create_project",
                               side_effect=app_projects.ValidationError("bad")):
            response = self._post("/admin/projects/create", {"id": "bad"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "bad"})

    def test_import_accepts_uploaded_file(self):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "set_actor"), \
                mock.patch.object(appmod.app_projects, "import_quotas",
                                  return_value={"rows": 1}) as import_quotas:
            response = self.http.post(
                "/admin/projects/import", headers=ADMIN,
                data={"file": (BytesIO(b"project-id\np\n"), "quotas.csv")},
                content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(import_quotas.call_args.args[2], b"project-id\np\n")

    def test_import_accepts_json_csv(self):
        with mock.patch.object(appmod.app_projects, "import_quotas",
                               return_value={"rows": 1}):
            response = self._post("/admin/projects/import", {"csv": "x"})
        self.assertEqual(response.status_code, 200)

    def test_manager_scope_is_passed_through(self):
        from ycluster.utils import authentik_manager
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "set_actor"), \
                mock.patch.object(authentik_manager, "manager_projects",
                                  return_value=["acme"]), \
                mock.patch.object(appmod.app_projects, "update_project") as upd:
            upd.return_value = {"id": "p"}
            self.http.post("/admin/projects/update", headers=MANAGER,
                           json={"id": "p", "name": "n"})
        self.assertEqual(upd.call_args.args[-1], ["acme"])


class ProjectAuditTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_admin_sees_unscoped_events(self):
        events = [{"id": 1, "entity_id": "p"}]
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "audit_log",
                                  return_value=events) as log:
            response = self.http.get("/admin/projects/audit?limit=5",
                                     headers=ADMIN)
        self.assertEqual(response.get_json(), {"events": events})
        self.assertEqual(log.call_args.args[2], 5)

    def test_manager_events_are_filtered_to_scope(self):
        from ycluster.utils import authentik_manager
        events = [{"entity_id": "p/member/x"}, {"entity_id": "other"}]
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(authentik_manager, "manager_projects",
                                  return_value=["acme"]), \
                mock.patch.object(appmod.app_projects,
                                  "list_projects_for_idp",
                                  return_value=[{"id": "p"}]), \
                mock.patch.object(appmod.app_projects, "audit_log",
                                  return_value=events):
            response = self.http.get("/admin/projects/audit",
                                     headers=MANAGER)
        self.assertEqual(response.get_json(),
                         {"events": [{"entity_id": "p/member/x"}]})

    def test_validation_error_returns_400(self):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "audit_log",
                                  side_effect=app_projects.ValidationError(
                                      "bad id")):
            response = self.http.get("/admin/projects/audit",
                                     headers=ADMIN)
        self.assertEqual(response.status_code, 400)

    def test_database_error_returns_503(self):
        with mock.patch.object(appmod.app_project_db, "connect",
                               return_value=FakeConn()), \
                mock.patch.object(appmod.app_projects, "audit_log",
                                  side_effect=RuntimeError("down")), \
                mock.patch.object(appmod.app.logger, "exception"):
            response = self.http.get("/admin/projects/audit",
                                     headers=ADMIN)
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
