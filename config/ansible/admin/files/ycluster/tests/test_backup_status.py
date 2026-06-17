"""Unit tests for ycluster.utils.backup_status (backup freshness / verify metrics).

Pure rendering + scanning logic only. The module is loaded by file path rather
than `from ycluster.utils import ...` so these run without the package __init__
(which imports etcd3, a cluster-only dep) — the logic here touches neither etcd
nor a socket.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest tests.test_backup_status
"""

import importlib.util
import os
import pathlib
import tempfile
import unittest

_MOD = pathlib.Path(__file__).resolve().parents[1] / "ycluster" / "utils" / "backup_status.py"
_spec = importlib.util.spec_from_file_location("backup_status", _MOD)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


class NewestAgeTests(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(bs.newest_age_seconds([], now=1000))

    def test_picks_newest(self):
        self.assertEqual(bs.newest_age_seconds([100, 900, 500], now=1000), 100)

    def test_clamps_future_mtime_to_zero(self):
        # clock skew: a backup stamped slightly in the future must not go negative
        self.assertEqual(bs.newest_age_seconds([1100], now=1000), 0.0)


class ScanAgesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _touch(self, component, name, mtime):
        d = os.path.join(self.dir, component)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        open(p, "w").close()
        os.utime(p, (mtime, mtime))

    def test_missing_dir_is_none(self):
        ages = bs.scan_ages(self.dir, now=1000)
        self.assertIsNone(ages["postgres"])
        self.assertIsNone(ages["qdrant"])
        self.assertIsNone(ages["etcd"])

    def test_newest_age_and_ignores_non_age(self):
        self._touch("postgres", "old.sql.gz.age", 100)
        self._touch("postgres", "new.sql.gz.age", 800)
        self._touch("postgres", "scratch.tmp", 999)  # not a .age file
        ages = bs.scan_ages(self.dir, now=1000)
        self.assertEqual(ages["postgres"], 200)

    def test_ignores_unrelated_component_dirs(self):
        self._touch("etcd", "x.db.age", 950)
        ages = bs.scan_ages(self.dir, now=1000)
        self.assertEqual(ages["etcd"], 50)
        self.assertIsNone(ages["postgres"])


class ParseResultsTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(
            bs.parse_results(["postgres=1", "etcd=0"]),
            {"postgres": True, "etcd": False},
        )

    def test_skip_maps_to_none(self):
        self.assertEqual(
            bs.parse_results(["postgres=1", "qdrant=skip", "etcd=0"]),
            {"postgres": True, "qdrant": None, "etcd": False},
        )

    def test_none_input(self):
        self.assertEqual(bs.parse_results(None), {})

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            bs.parse_results(["postgres=yes"])

    def test_missing_value_raises(self):
        with self.assertRaises(ValueError):
            bs.parse_results(["postgres"])


class RenderMetricsTests(unittest.TestCase):
    def test_full_render(self):
        ages = {"postgres": 60, "qdrant": 120, "etcd": 60}
        results = {"postgres": True, "qdrant": True, "etcd": True}
        out = bs.render_metrics(1718000000.5, ages, results)
        self.assertIn('ycluster_backup_restore_success{component="postgres"} 1', out)
        self.assertIn('ycluster_backup_age_seconds{component="qdrant"} 120', out)
        # timestamp truncated to int
        self.assertIn("ycluster_backup_restore_timestamp_seconds 1718000000", out)
        self.assertTrue(out.endswith("\n"))

    def test_failed_component_emits_zero(self):
        out = bs.render_metrics(1000, {"etcd": 10}, {"etcd": False})
        self.assertIn('ycluster_backup_restore_success{component="etcd"} 0', out)

    def test_skipped_component_emits_no_success_sample(self):
        # result None => no success line (so the alert can't fire on a skip)
        out = bs.render_metrics(1000, {"postgres": 10}, {"postgres": None})
        self.assertNotIn("ycluster_backup_restore_success{", out)
        # but freshness is still reported
        self.assertIn('ycluster_backup_age_seconds{component="postgres"} 10', out)

    def test_missing_age_emits_no_age_sample(self):
        out = bs.render_metrics(1000, {"postgres": None}, {"postgres": True})
        self.assertNotIn("ycluster_backup_age_seconds{", out)


class WriteAtomicTests(unittest.TestCase):
    def test_write_atomic_no_tmp_left(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "backup_restore.prom")
        bs.write_atomic(path, "hello\n")
        with open(path) as f:
            self.assertEqual(f.read(), "hello\n")
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
