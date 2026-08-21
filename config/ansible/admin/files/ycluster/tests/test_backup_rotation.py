"""Unit tests for ycluster.utils.backup_rotation (offsite retention policy).

Pure decision logic only — same load-by-path pattern as test_backup_status.py
so these run without the package __init__ (which imports etcd3).
"""

import datetime as _dt
import importlib.util
import pathlib
import unittest

_MOD = pathlib.Path(__file__).resolve().parents[1] / "ycluster" / "utils" / "backup_rotation.py"
_spec = importlib.util.spec_from_file_location("backup_rotation", _MOD)
assert _spec is not None
br = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(br)

TODAY = _dt.date(2026, 8, 21)


def _entry(path, size=1000, mtime=0.0):
    return {"path": path, "size": size, "mtime": mtime,
            "date": br.parse_backup_date(path), "engine": br._engine_of(path)}


class ParseBackupDateTests(unittest.TestCase):
    def test_parses_leading_yyyymmdd(self):
        self.assertEqual(br.parse_backup_date("20260821_002520.db.age"),
                         _dt.date(2026, 8, 21))
        self.assertEqual(br.parse_backup_date("20260817_003517.sql.gz.age"),
                         _dt.date(2026, 8, 17))

    def test_rejects_garbage(self):
        self.assertIsNone(br.parse_backup_date("./etcd/notadate.db.age"))
        self.assertIsNone(br.parse_backup_date("2026_081.db.age"))
        self.assertIsNone(br.parse_backup_date("999999_1901.db.age"))  # bad month


class ScheduleKeepTests(unittest.TestCase):
    def _days(self, n, engine="etcd"):
        # n consecutive days ending today
        files = []
        for off in range(n):
            d = TODAY - _dt.timedelta(days=off)
            name = "%s_000000.db.age" % d.strftime("%Y%m%d")
            files.append(_entry("./%s/%s" % (engine, name)))
        return files

    def test_keeps_all_when_under_daily_window(self):
        files = self._days(3)
        keep = br.schedule_keep(files, TODAY)
        self.assertEqual(keep, {f["path"] for f in files})

    def test_keeps_exactly_daily_days_of_most_recent(self):
        files = self._days(20)
        keep = br.schedule_keep(files, TODAY)
        newest = {f["path"] for f in files[:br.DEFAULT_DAILY_DAYS]}
        self.assertTrue(newest <= keep)
        # every most-recent daily-day path is kept
        self.assertEqual(len(keep & newest), br.DEFAULT_DAILY_DAYS)

    def test_sparse_weekly_slot(self):
        # 20 days of one engine = 7 daily + ~3 ISO weeks (within weekly window)
        files = self._days(20)
        keep = br.schedule_keep(files, TODAY)
        # distinct iso-weeks covered by the *extra* (non-daily) files
        extras = files[br.DEFAULT_DAILY_DAYS:]
        extra_weeks = {(f["date"].isocalendar()[0], f["date"].isocalendar()[1])
                       for f in extras}
        kept_extras = {f["path"] for f in extras if f["path"] in keep}
        # at most one per week among the sparsed region
        self.assertEqual(len(kept_extras), len(extra_weeks))

    def test_undated_never_scheduled_for_delete(self):
        files = self._days(20)
        undated = _entry("./etcd/no_date_here.db.age")
        files.append(undated)
        keep = br.schedule_keep(files, TODAY)
        self.assertIn(undated["path"], keep)

    def test_monthly_tier_past_weekly_window(self):
        # 300 days back -> beyond weekly window, so monthly tier kicks in
        files = []
        for off in range(300):
            d = TODAY - _dt.timedelta(days=off)
            name = "%s_000000.db.age" % d.strftime("%Y%m%d")
            files.append(_entry("./etcd/%s" % name))
        keep = br.schedule_keep(files, TODAY)
        deleted = {f["path"] for f in files} - keep
        self.assertTrue(deleted)  # sparsing happened


class PlanTests(unittest.TestCase):
    def test_plan_keeps_daily_window_and_sparsed_older(self):
        files = []
        for off in range(300):
            d = TODAY - _dt.timedelta(days=off)
            name = "%s_000000.db.age" % d.strftime("%Y%m%d")
            files.append(_entry("./etcd/%s" % name, size=100_000))
        res = br.plan(files, today=TODAY)
        paths = {f["path"] for f in files}
        self.assertTrue(set(res["delete"]) <= paths)
        self.assertEqual(res["delete_bytes"],
                         sum(f["size"] for f in files if f["path"] in res["delete"]))
        # newest 7 are never deleted regardless of disk size
        newest7 = {f["path"] for f in files[:br.DEFAULT_DAILY_DAYS]}
        self.assertTrue(newest7.isdisjoint(res["delete"]))

    def test_plan_splits_by_engine(self):
        # Each engine's retention is computed independently: one per ISO week
        # beyond its own 7 daily files, so per-week duplicates across engines
        # never cause a newer engine's window to be dropped.
        files = []
        for eng in ("etcd", "postgres", "qdrant"):
            for off in range(60):
                d = TODAY - _dt.timedelta(days=off)
                name = "%s_000000.db.age" % d.strftime("%Y%m%d")
                files.append(_entry("./%s/%s" % (eng, name), size=1000))
        res = br.plan(files, today=TODAY)
        deleted = set(res["delete"])
        for eng in ("etcd", "postgres", "qdrant"):
            eng_files = sorted((f for f in files if f["path"].startswith("./%s/" % eng)),
                               key=lambda f: f["date"], reverse=True)
            newest7 = {f["path"] for f in eng_files[:br.DEFAULT_DAILY_DAYS]}
            self.assertEqual(newest7 & deleted, set(),
                             "daily window of %s was scheduled for deletion" % eng)

    def test_plan_empty(self):
        res = br.plan([], today=TODAY)
        self.assertEqual(res["delete"], [])
        self.assertEqual(res["kept"], 0)


if __name__ == "__main__":
    unittest.main()
