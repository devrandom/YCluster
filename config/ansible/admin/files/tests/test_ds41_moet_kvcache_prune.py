"""Tests for the DS4.1 MoET filesystem KV tier pruner."""

import contextlib
import importlib.util
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_FILES = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "ds41_moet_kvcache_prune", _FILES / "ds41-moet-kvcache-prune.py")
kvcache_prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kvcache_prune)

_SHARD = Path("_models_abc_r0") / "ab" / "ab_g0"


class PruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "v41-interleaved"
        (self.root / _SHARD).mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def block(self, name, size, age):
        path = self.root / _SHARD / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def run_prune(self, max_bytes, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kvcache_prune.main(
                ["--root", str(self.root),
                 "--max-bytes", str(max_bytes),
                 *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_under_budget_keeps_everything(self):
        kept = self.block("a.bin", 400, 100)
        rc, out, _err = self.run_prune(1000, "--min-age-seconds", "0")
        self.assertEqual(rc, 0)
        self.assertTrue(kept.exists())
        self.assertIn("nothing to prune", out)

    def test_deletes_oldest_until_under_budget(self):
        oldest = self.block("a.bin", 400, 300)
        middle = self.block("b.bin", 400, 200)
        newest = self.block("c.bin", 400, 100)
        rc, out, _err = self.run_prune(800, "--min-age-seconds", "0")
        self.assertEqual(rc, 0)
        self.assertFalse(oldest.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(newest.exists())
        self.assertIn("freed 0.0 GiB in 1 blocks", out)

    def test_temp_files_are_not_candidates(self):
        in_flight = self.block("d.bin.tmp", 900, 100)
        old = self.block("a.bin", 500, 100)
        rc, _out, _err = self.run_prune(1000, "--min-age-seconds", "0")
        self.assertEqual(rc, 0)
        self.assertTrue(in_flight.exists())
        self.assertTrue(old.exists())

    def test_recent_blocks_are_left_alone(self):
        old = self.block("a.bin", 500, 7200)
        recent = self.block("b.bin", 500, 10)
        rc, _out, err = self.run_prune(100, "--min-age-seconds", "3600")
        self.assertEqual(rc, 0)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertIn("budget still exceeded", err)

    def test_missing_root_is_not_an_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = kvcache_prune.main(
                ["--root", str(Path(self._tmp.name) / "absent"),
                 "--max-bytes", "1000"])
        self.assertEqual(rc, 0)
        self.assertIn("not present", out.getvalue())

    def test_dry_run_deletes_nothing(self):
        oldest = self.block("a.bin", 400, 100)
        self.block("b.bin", 400, 200)
        rc, out, _err = self.run_prune(
            400, "--min-age-seconds", "0", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(oldest.exists())
        self.assertIn("would free", out)

    def test_unlink_failure_is_reported(self):
        self.block("a.bin", 400, 100)
        with mock.patch.object(
                kvcache_prune.os, "unlink", side_effect=PermissionError("nope")):
            rc, _out, err = self.run_prune(100, "--min-age-seconds", "0")
        self.assertEqual(rc, 1)
        self.assertIn("cannot remove", err)


if __name__ == "__main__":
    unittest.main()
