#!/usr/bin/env python3
"""Unit tests for the osv-feed textfile collector.

Run:  python3 -m unittest test_osv_feed_collector  (from this dir)
"""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "osv_feed_collector", os.path.join(_HERE, "osv-feed-collector.py")
)
col = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(col)


class MirrorHealth(unittest.TestCase):
    def test_empty_db_reports_missing(self):
        with tempfile.TemporaryDirectory() as d:
            present, age = col.mirror_health(d)
        self.assertFalse(present)
        self.assertEqual(age, 0)

    def test_newest_cursor_drives_age(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "Ubuntu.cursor")
            new = os.path.join(d, "Debian.cursor")
            with open(old, "w") as fh:
                fh.write("x")
            with open(new, "w") as fh:
                fh.write("x")
            cut = time.time() - 100
            os.utime(old, (cut, cut))          # Ubuntu: 100s old
            os.utime(new, (cut + 50, cut + 50))  # Debian: 50s old
            present, age = col.mirror_health(d)
            self.assertTrue(present)
            # The most-stale cursor (Ubuntu) drives the reported age.
            self.assertGreater(age, 95)
            self.assertLess(age, 110)


class RunMatch(unittest.TestCase):
    def test_success_returns_body(self):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = "osv_feed_cve_info{...} 1\n"
        proc.stderr = "warning line\n"
        with mock.patch.object(col.subprocess, "run", return_value=proc) as run:
            ok, body, errors = col.run_match()
        run.assert_called_once()
        self.assertTrue(ok)
        self.assertIn("osv_feed_cve_info", body)
        self.assertEqual(errors, ["warning line"])

    def test_failure_returns_no_body(self):
        proc = mock.Mock()
        proc.returncode = 1
        proc.stdout = "partial garbage"
        proc.stderr = "boom\n"
        with mock.patch.object(col.subprocess, "run", return_value=proc):
            ok, body, errors = col.run_match()
        self.assertFalse(ok)
        self.assertEqual(body, "")
        self.assertEqual(errors, ["boom"])

    def test_spawn_failure(self):
        def boom(*a, **k):
            raise OSError("no such file")

        with mock.patch.object(col.subprocess, "run", side_effect=boom):
            ok, body, errors = col.run_match()
        self.assertFalse(ok)
        self.assertEqual(body, "")
        self.assertEqual(errors, ["no such file"])

    def test_timeout(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired([], 10)

        with mock.patch.object(col.subprocess, "run", side_effect=boom):
            ok, body, errors = col.run_match()
        self.assertFalse(ok)
        self.assertEqual(body, "")


class WriteProm(unittest.TestCase):
    def test_writes_body_and_health_gauges(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "osv_feed.prom")
            with mock.patch.object(col, "OUT", out):
                col.write_prom(True, 3600, True, "osv_feed_cve_info{...} 1\n", [])
            text = open(out).read()
            self.assertIn("osv_feed_cve_info{...} 1", text)
            self.assertIn("osv_feed_mirror_present 1", text)
            self.assertIn("osv_feed_mirror_age_seconds 3600", text)
            self.assertIn("osv_feed_collector_error 0", text)

    def test_failed_match_writes_error_gauge_and_no_cve_body(self):
        # run_match returns an empty body on failure; write_prom then emits
        # only the health gauges plus the error comment, never stale series.
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "osv_feed.prom")
            with mock.patch.object(col, "OUT", out):
                col.write_prom(True, 100, False, "", ["boom"])
            text = open(out).read()
            self.assertNotIn("osv_feed_cve_info", text)
            self.assertIn("osv_feed_collector_error 1", text)
            self.assertIn("boom", text)

    def test_missing_feed_reports_present_zero(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "osv_feed.prom")
            with mock.patch.object(col, "OUT", out):
                col.write_prom(False, 0, True, "", [])
            text = open(out).read()
            self.assertIn("osv_feed_mirror_present 0", text)
            self.assertIn("osv_feed_mirror_age_seconds 0", text)


if __name__ == "__main__":
    unittest.main()
