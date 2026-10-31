"""Coverage tests for the project/quota domain layer (app_projects.py).

The route tests live in test_app_projects.py; these exercise the validation,
scoping and SQL-shaping branches directly against a recording cursor.
"""

import unittest
from decimal import Decimal
from unittest import mock

import app_projects
from app_projects import ValidationError


class Identity:
    uid = "op-uid"
    email = "op@example.test"


def project_row(pid="p", idp="acme", active=True):
    return (pid, "Project", "", idp, active, None, None)


class FakeCursor:
    """Records SQL and returns one queued result per execute()."""

    def __init__(self, results=(), rowcount=1):
        self.results = list(results)
        self.executed = []
        self.rowcount = rowcount
        self.current = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.current = self.results.pop(0) if self.results else None

    def fetchone(self):
        if self.current is None:
            return None
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return list(self.current or [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ValidationTests(unittest.TestCase):
    def test_project_id_rejects_invalid(self):
        with self.assertRaises(ValidationError):
            app_projects.clean_project_id("Bad Id")

    def test_text_required_and_length(self):
        with self.assertRaises(ValidationError):
            app_projects.clean_text("", "name", required=True)
        with self.assertRaises(ValidationError):
            app_projects.clean_text("x" * 5, "name", max_length=4)

    def test_quota_value_rejects_bad_input(self):
        for bad in ("nope", "-1", "1.00001", "1" + "0" * 15):
            with self.assertRaises(ValidationError):
                app_projects.clean_quota_value(bad)
        self.assertEqual(app_projects.clean_quota_value("1.5000"),
                         Decimal("1.5"))

    def test_row_helpers_accept_empty(self):
        self.assertIsNone(app_projects._project_row(None))
        self.assertIsNone(app_projects._quota_row(None))


class ProjectCrudTests(unittest.TestCase):
    def test_create_rejects_duplicate(self):
        cur = FakeCursor(results=[[project_row()]])
        with self.assertRaises(ValidationError):
            app_projects.create_project(cur, Identity(), "p", "Project")

    def test_create_project_success(self):
        cur = FakeCursor(results=[None, [project_row()]])
        created = app_projects.create_project(cur, Identity(), "p", "Project")
        self.assertEqual(created["id"], "p")

    def test_update_missing_project(self):
        cur = FakeCursor(results=[None])
        with self.assertRaises(ValidationError):
            app_projects.update_project(cur, Identity(), "p", name="New")

    def test_update_rejects_move_outside_scope(self):
        cur = FakeCursor(results=[[project_row()], ["other"]])
        with self.assertRaises(ValidationError):
            app_projects.update_project(
                cur, Identity(), "p", idp_project="other",
                allowed_idp_projects={"acme"})

    def test_update_without_changes_returns_current(self):
        cur = FakeCursor(results=[[project_row()]])
        current = app_projects.update_project(cur, Identity(), "p")
        self.assertEqual(current["id"], "p")
        self.assertEqual(len(cur.executed), 1)

    def test_update_writes_every_field(self):
        cur = FakeCursor(results=[[project_row()], None,
                                  [project_row(idp="acme2", active=False)]])
        updated = app_projects.update_project(
            cur, Identity(), "p", name="New", description="d",
            active=False, idp_project="acme2")
        self.assertFalse(updated["active"])
        sql, params = cur.executed[1]
        self.assertIn("UPDATE projects SET", sql)
        self.assertIn(False, params)

    def test_update_rejects_non_boolean_active(self):
        cur = FakeCursor(results=[[project_row()]])
        with self.assertRaises(ValidationError):
            app_projects.update_project(cur, Identity(), "p", active="yes")


class MemberTests(unittest.TestCase):
    def test_list_members(self):
        row = ("p", "uid-1", "a@example.test", True, None)
        cur = FakeCursor(results=[[row]])
        self.assertEqual(app_projects.list_members(cur, "p"),
                         [{"project_id": "p", "authentik_uid": "uid-1",
                           "email_at_assignment": "a@example.test",
                           "is_default": True, "created_at": None}])

    def test_list_quotas(self):
        cur = FakeCursor(results=[[("p", "gpu-hours", "q", Decimal("2"),
                                    None)]])
        self.assertEqual(app_projects.list_quotas(cur, "p")[0]["value"], "2")

    def test_add_member_requires_project(self):
        cur = FakeCursor(results=[None])
        with self.assertRaises(ValidationError):
            app_projects.add_member(cur, Identity(), "p", "u", "e")

    def test_add_member_defaults_email_and_flag(self):
        cur = FakeCursor(results=[[project_row()], None, None])
        app_projects.add_member(cur, Identity(), "p", "uid-1", "")
        _sql, params = cur.executed[-1]
        self.assertEqual(params[2], "uid-1")
        self.assertFalse(params[3])

    def test_add_member_treats_none_default_as_false(self):
        cur = FakeCursor(results=[[project_row()], None, None])
        app_projects.add_member(cur, Identity(), "p", "u", "e",
                                is_default=None)
        self.assertFalse(cur.executed[-1][1][3])

    def test_add_member_rejects_non_boolean_default(self):
        cur = FakeCursor(results=[[project_row()]])
        with self.assertRaises(ValidationError):
            app_projects.add_member(cur, Identity(), "p", "u", "e",
                                    is_default="yes")

    def test_add_member_promotes_default(self):
        cur = FakeCursor(results=[[project_row()], (False,), None])
        app_projects.add_member(cur, Identity(), "p", "u", "e",
                                is_default=True)
        self.assertIn("UPDATE project_members SET is_default = FALSE",
                      cur.executed[2][0])

    def test_remove_member_missing(self):
        cur = FakeCursor(results=[None], rowcount=0)
        with self.assertRaises(ValidationError):
            app_projects.remove_member(cur, Identity(), "p", "u")

    def test_set_default_creates_missing_membership(self):
        cur = FakeCursor(results=[None])
        with mock.patch.object(app_projects, "add_member") as add:
            app_projects.set_default_member(cur, Identity(), "p", "u",
                                            "e@example.test")
        add.assert_called_once()

    def test_set_default_reports_missing_membership(self):
        cur = FakeCursor(results=[(1,), None, None], rowcount=0)
        with self.assertRaises(ValidationError):
            app_projects.set_default_member(cur, Identity(), "p", "u")


class QuotaTests(unittest.TestCase):
    def test_upsert_requires_project(self):
        cur = FakeCursor(results=[None])
        with self.assertRaises(ValidationError):
            app_projects.upsert_quota(cur, Identity(), "p", "t", "q", "1")

    def test_upsert_requires_quota_type(self):
        cur = FakeCursor(results=[[project_row()], None])
        with self.assertRaises(ValidationError):
            app_projects.upsert_quota(cur, Identity(), "p", "t", "q", "1")

    def test_upsert_returns_row(self):
        cur = FakeCursor(results=[[project_row()], (1,)])
        row = app_projects.upsert_quota(cur, Identity(), "p", "gpu-hours",
                                        "q", "1.5")
        self.assertEqual(row, {"project_id": "p", "quota_type": "gpu-hours",
                               "quota_id": "q", "value": "1.5",
                               "updated_at": None})

    def test_delete_missing_quota(self):
        cur = FakeCursor(results=[None], rowcount=0)
        with self.assertRaises(ValidationError):
            app_projects.delete_quota(cur, Identity(), "p", "t", "q")


class CsvTests(unittest.TestCase):
    header = "project-id,quota-type,quota-id,value\n"

    def parse(self, text):
        return app_projects.parse_quota_csv(text)

    def test_rejects_bad_header(self):
        with self.assertRaises(ValidationError):
            self.parse("a,b\n1,2\n")

    def test_rejects_duplicate_rows(self):
        with self.assertRaises(ValidationError) as ctx:
            self.parse(self.header + "p,gpu-hours,q,1\np,gpu-hours,q,2\n")
        self.assertIn("row 3", str(ctx.exception))

    def test_rejects_empty_input(self):
        with self.assertRaises(ValidationError):
            self.parse(self.header)

    def test_rejects_too_many_rows(self):
        rows = "".join(f"p{i},gpu-hours,q,1\n" for i in range(
            app_projects.MAX_IMPORT_ROWS + 1))
        with self.assertRaises(ValidationError):
            self.parse(self.header + rows)

    def test_accepts_bytes(self):
        rows = self.parse((self.header + "p,gpu-hours,q,1\n").encode())
        self.assertEqual(rows[0]["value"], Decimal("1"))

    def test_rejects_invalid_utf8(self):
        with self.assertRaises(ValidationError):
            self.parse(b"\xff\xfe")


class ImportTests(unittest.TestCase):
    text = "project-id,quota-type,quota-id,value\np,gpu-hours,q,1\n"

    def test_rejects_unknown_project(self):
        cur = FakeCursor(results=[[("other",)]])
        with self.assertRaises(ValidationError):
            app_projects.import_quotas(cur, Identity(), self.text)

    def test_rejects_project_outside_scope(self):
        cur = FakeCursor(results=[[("p",)], []])
        with self.assertRaises(ValidationError):
            app_projects.import_quotas(cur, Identity(), self.text,
                                       allowed_idp_projects={"acme"})

    def test_rejects_unknown_quota_type(self):
        cur = FakeCursor(results=[[("p",)], [("other",)]])
        with self.assertRaises(ValidationError):
            app_projects.import_quotas(cur, Identity(), self.text)

    def test_skips_unchanged_rows(self):
        cur = FakeCursor(results=[[("p",)], [("gpu-hours",)], None,
                                  [(Decimal("1"),)]])
        result = app_projects.import_quotas(cur, Identity(), self.text)
        self.assertEqual(result["changed"], 0)

    def test_applies_changed_rows(self):
        cur = FakeCursor(results=[[("p",)], [("gpu-hours",)], None,
                                  [(Decimal("2"),)]])
        result = app_projects.import_quotas(cur, Identity(), self.text)
        self.assertEqual(result["changed"], 1)
        self.assertIn("INSERT INTO audit_events", cur.executed[-1][0])


class AuditLogTests(unittest.TestCase):
    row = (1, None, "op", "op@example.test", "update", "projects",
           "project", "p", None, "{}", None, None)

    def test_project_scoped_query(self):
        cur = FakeCursor(results=[[self.row]])
        events = app_projects.audit_log(cur, "p", 5000)
        self.assertEqual(events[0]["id"], 1)
        self.assertEqual(cur.executed[0][1]["limit"], 1000)

    def test_unscoped_query_clamps_limit(self):
        cur = FakeCursor(results=[[self.row]])
        app_projects.audit_log(cur)
        self.assertEqual(cur.executed[0][1], (200,))


if __name__ == "__main__":
    unittest.main()
