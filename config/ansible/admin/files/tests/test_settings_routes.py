"""Route tests for the extracted self-service settings/usage endpoints."""

import unittest
from unittest import mock

import app as appmod


USER = {
    "X-Authentik-UID": "uid-user",
    "X-Authentik-Email": "user@example.test",
}
ANON = {"X-Authentik-UID": "uid-user"}
ADMIN = {
    "X-Authentik-UID": "uid-admin",
    "X-Authentik-Email": "admin@example.test",
    "X-Authentik-Groups": "ycluster-admins",
}


class FakeCursor:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=None):
        self.rows = []

    def fetchall(self):
        return self.rows

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


class SettingsDataTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_page_renders(self):
        self.assertEqual(self.http.get("/admin/settings").status_code, 200)

    def test_requires_authenticated_email(self):
        self.assertEqual(
            self.http.get("/admin/settings/data", headers=ANON).status_code,
            403)

    def test_returns_keys(self):
        rec = {"ssh_keys": ["ssh-ed25519 AAAA"]}
        with mock.patch.object(appmod.vmm, "user_get", return_value=rec):
            response = self.http.get("/admin/settings/data", headers=USER)
        self.assertEqual(response.get_json()["keys"], rec["ssh_keys"])

    def test_etcd_failure_reports_503(self):
        with mock.patch.object(appmod.vmm, "user_get",
                               side_effect=RuntimeError("down")):
            response = self.http.get("/admin/settings/data", headers=USER)
        self.assertEqual(response.status_code, 503)


class SettingsKeyTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_add_requires_email(self):
        self.assertEqual(
            self.http.post("/admin/settings/keys/add", headers=ANON,
                           json={"key": "k"}).status_code, 403)

    def test_add_requires_key(self):
        self.assertEqual(
            self.http.post("/admin/settings/keys/add", headers=USER,
                           json={}).status_code, 400)

    def test_add_rejects_bad_key(self):
        with mock.patch.object(appmod.vmm, "user_add_key",
                               side_effect=ValueError("bad key")):
            response = self.http.post("/admin/settings/keys/add",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.status_code, 400)

    def test_add_reports_write_failure(self):
        with mock.patch.object(appmod.vmm, "user_add_key",
                               side_effect=RuntimeError("down")):
            response = self.http.post("/admin/settings/keys/add",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.status_code, 503)

    def test_add_returns_fresh_keys(self):
        with mock.patch.object(appmod.vmm, "user_add_key"), \
                mock.patch.object(appmod.vmm, "user_get",
                                  return_value={"ssh_keys": ["k"]}):
            response = self.http.post("/admin/settings/keys/add",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.get_json(), {"keys": ["k"]})

    def test_remove_requires_key(self):
        self.assertEqual(
            self.http.post("/admin/settings/keys/remove", headers=USER,
                           json={}).status_code, 400)

    def test_remove_reports_missing_key(self):
        with mock.patch.object(appmod.vmm, "user_get",
                               return_value={"ssh_keys": []}):
            response = self.http.post("/admin/settings/keys/remove",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.status_code, 503)

    def test_remove_rejects_bad_key(self):
        with mock.patch.object(appmod.vmm, "user_get",
                               return_value={"ssh_keys": ["k"]}), \
                mock.patch.object(appmod.vmm, "user_remove_key",
                                  side_effect=ValueError("bad key")):
            response = self.http.post("/admin/settings/keys/remove",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.status_code, 400)

    def test_remove_succeeds(self):
        with mock.patch.object(appmod.vmm, "user_get",
                               side_effect=[{"ssh_keys": ["k"]},
                                            {"ssh_keys": []}]), \
                mock.patch.object(appmod.vmm, "user_remove_key") as remove:
            response = self.http.post("/admin/settings/keys/remove",
                                      headers=USER, json={"key": "k"})
        self.assertEqual(response.get_json(), {"keys": []})
        remove.assert_called_once_with("user@example.test", "k")


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_page_renders(self):
        self.assertEqual(
            self.http.get("/admin/settings/api-keys").status_code, 200)

    def test_data_requires_email(self):
        self.assertEqual(
            self.http.get("/admin/settings/api-keys/data",
                          headers=ANON).status_code, 403)

    def test_data_reports_failure(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               side_effect=RuntimeError("down")):
            response = self.http.get("/admin/settings/api-keys/data",
                                     headers=USER)
        self.assertEqual(response.status_code, 503)

    def test_data_returns_keys(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               return_value={"user@example.test": [{"key_id": "k"}]}):
            response = self.http.get("/admin/settings/api-keys/data",
                                     headers=USER)
        self.assertEqual(response.get_json()["keys"], [{"key_id": "k"}])

    def test_create_requires_email(self):
        self.assertEqual(
            self.http.post("/admin/settings/api-keys/create", headers=ANON,
                           json={}).status_code, 403)

    def test_create_rejects_bad_label(self):
        response = self.http.post("/admin/settings/api-keys/create",
                                  headers=USER, json={"label": 5})
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_invalid_key(self):
        with mock.patch.object(appmod.apikeys, "create_key",
                               side_effect=ValueError("bad")):
            response = self.http.post("/admin/settings/api-keys/create",
                                      headers=USER, json={"label": "l"})
        self.assertEqual(response.status_code, 400)

    def test_create_reports_write_failure(self):
        with mock.patch.object(appmod.apikeys, "create_key",
                               side_effect=RuntimeError("down")):
            response = self.http.post("/admin/settings/api-keys/create",
                                      headers=USER, json={"label": "l"})
        self.assertEqual(response.status_code, 503)

    def test_create_returns_secret(self):
        with mock.patch.object(appmod.apikeys, "create_key",
                               return_value="sk-secret"):
            response = self.http.post("/admin/settings/api-keys/create",
                                      headers=USER, json={"label": "laptop"})
        self.assertEqual(response.get_json(),
                         {"secret": "sk-secret", "label": "laptop"})

    def test_revoke_requires_email(self):
        self.assertEqual(
            self.http.post("/admin/settings/api-keys/revoke", headers=ANON,
                           json={"key_id": "k"}).status_code, 403)

    def test_revoke_requires_key_id(self):
        self.assertEqual(
            self.http.post("/admin/settings/api-keys/revoke", headers=USER,
                           json={}).status_code, 400)

    def test_revoke_rejects_foreign_key(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               return_value={"user@example.test": []}):
            response = self.http.post("/admin/settings/api-keys/revoke",
                                      headers=USER, json={"key_id": "k"})
        self.assertEqual(response.status_code, 404)

    def test_revoke_rejects_bad_key(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               return_value={"user@example.test": [
                                   {"key_id": "k"}]}), \
                mock.patch.object(appmod.apikeys, "revoke_key",
                                  side_effect=ValueError("bad")):
            response = self.http.post("/admin/settings/api-keys/revoke",
                                      headers=USER, json={"key_id": "k"})
        self.assertEqual(response.status_code, 400)

    def test_revoke_reports_write_failure(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               side_effect=RuntimeError("down")):
            response = self.http.post("/admin/settings/api-keys/revoke",
                                      headers=USER, json={"key_id": "k"})
        self.assertEqual(response.status_code, 503)

    def test_revoke_succeeds(self):
        with mock.patch.object(appmod.apikeys, "list_keys",
                               return_value={"user@example.test": [
                                   {"key_id": "k"}]}), \
                mock.patch.object(appmod.apikeys, "revoke_key") as revoke:
            response = self.http.post("/admin/settings/api-keys/revoke",
                                      headers=USER, json={"key_id": "k"})
        self.assertEqual(response.get_json(), {"ok": True})
        revoke.assert_called_once_with("k")


class UsageRouteTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_vm_usage_page_renders(self):
        with mock.patch("settings_routes._vm_usage_since",
                            return_value="2026-01-01"):
            self.assertEqual(
                self.http.get("/admin/vm-usage", headers=ADMIN).status_code,
                200)

    def test_vm_usage_data_reports_cursor_error(self):
        with mock.patch("settings_routes._vm_usage_window",
                            return_value=("a", "b")), \
                mock.patch.object(appmod, "_usage_stats_cursor",
                                  return_value=(None, None, "no db")):
            response = self.http.get("/admin/vm-usage/data", headers=ADMIN)
        self.assertEqual(response.status_code, 500)

    def test_vm_usage_data_returns_rows(self):
        import app_usage
        with mock.patch("settings_routes._vm_usage_window",
                            return_value=("a", "b")), \
                mock.patch.object(appmod, "_usage_stats_cursor",
                                  return_value=(FakeConn(), None, None)), \
                mock.patch.object(app_usage, "fetch_usage_state",
                                  return_value=([], [], {})), \
                mock.patch.object(app_usage, "registry_snapshots",
                                  return_value=({}, [])), \
                mock.patch.object(app_usage, "owner_visibility",
                                  return_value=None), \
                mock.patch.object(app_usage, "usage_rows",
                                  return_value=[{"vm": "vm1"}]), \
                mock.patch.object(appmod, "_assemble_intervals",
                                  return_value={}), \
                mock.patch.object(appmod, "_usage_row"):
            response = self.http.get("/admin/vm-usage/data", headers=ADMIN)
        self.assertEqual(response.get_json(), [{"vm": "vm1"}])

    def test_model_usage_options_are_admin_only(self):
        self.assertEqual(
            self.http.get("/admin/model-usage/options",
                          headers=USER).status_code, 403)

    def test_model_usage_options_reports_cursor_error(self):
        with mock.patch.object(appmod, "_usage_stats_cursor",
                               return_value=(None, None, "no db")):
            response = self.http.get("/admin/model-usage/options",
                                     headers=ADMIN)
        self.assertEqual(response.status_code, 500)

    def test_model_usage_options_returns_values(self):
        with mock.patch.object(appmod, "_usage_stats_cursor",
                               return_value=(FakeConn(), None, None)):
            response = self.http.get("/admin/model-usage/options",
                                     headers=ADMIN)
        self.assertEqual(response.get_json(), {"users": [], "models": []})


class UsageWindowTests(unittest.TestCase):
    def test_since_accepts_known_values_only(self):
        import settings_routes
        with appmod.app.test_request_context("/?since=30"):
            self.assertEqual(settings_routes._vm_usage_since(), "30")
        with appmod.app.test_request_context("/?since=bogus"):
            self.assertEqual(settings_routes._vm_usage_since(), "7")

    def test_window_for_days(self):
        import settings_routes
        with appmod.app.test_request_context("/?since=30"):
            start, end = settings_routes._vm_usage_window(
                lambda days: f"{days}-days")
        self.assertEqual(start, "30-days")
        self.assertIsNotNone(end)

    def test_window_for_calendar_years(self):
        import settings_routes
        for since in ("this-year", "previous-year"):
            with appmod.app.test_request_context(f"/?since={since}"):
                start, end = settings_routes._vm_usage_window(
                    lambda days: "unused")
                self.assertEqual((start.month, start.day), (1, 1))
                self.assertGreaterEqual(end, start)


if __name__ == "__main__":
    unittest.main()
