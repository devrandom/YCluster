import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from osv_feed import cli
from osv_feed import kernel
from osv_feed import mirror

HIGH_CVSS = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def _write_record(db_dir, ecosystem, record):
    with mirror.connect(db_dir) as conn:
        mirror._insert_record(conn, ecosystem, record)


def _kernel_record(cve, entries, summary="kernel advisory"):
    return {
        "id": cve,
        "summary": summary,
        "severity": [{"type": "CVSS_V3", "score": HIGH_CVSS}],
        "affected": entries,
    }


def _linux_entry(versions, events):
    return {
        "package": {"ecosystem": "Ubuntu", "name": "linux"},
        "ranges": [{"type": "ECOSYSTEM", "events": events}],
        "versions": versions,
    }


class SeriesAndKeyTest(unittest.TestCase):
    def test_series_of_strips_abi(self):
        self.assertEqual("6.8.0", kernel.series_of("6.8.0-149.149"))
        self.assertEqual("7.0.0", kernel.series_of("7.0.0-43.43~24.04.1"))
        self.assertIsNone(kernel.series_of("0"))
        self.assertIsNone(kernel.series_of(""))

    def test_version_key_orders_builds_and_tilde_last(self):
        key = kernel.version_key
        self.assertLess(key("6.8.0-142.142"), key("6.8.0-149.149"))
        self.assertLess(key("7.0.0-42.42~24.04.1"), key("7.0.0-43.43~24.04.1"))
        # dpkg sorts a ~suffix before the plain upload of the same build.
        self.assertLess(key("7.0.0-43.43~24.04.1"), key("7.0.0-43.43"))
        self.assertIsNone(key("not-a-kernel-version"))


class BuildAndClassifyTest(unittest.TestCase):
    def setUp(self):
        self.record = _kernel_record(
            "UBUNTU-CVE-1",
            [
                _linux_entry(
                    ["6.8.0-11.11", "6.8.0-142.142", "6.8.0-147.147"],
                    [{"introduced": "0"}, {"fixed": "6.8.0-149.149"}],
                ),
                _linux_entry(
                    ["7.0.0-31.31", "7.0.0-42.42", "7.0.0-43.43~24.04.1"],
                    [{"introduced": "0"}],
                ),
            ],
        )

    def test_build_entries_splits_series(self):
        entries = kernel.build_entries(self.record, {"linux"})
        by_series = {entry["series"]: entry for entry in entries}
        self.assertEqual({"6.8.0", "7.0.0"}, set(by_series))
        self.assertEqual("versions", by_series["6.8.0"]["mode"])
        self.assertTrue(by_series["6.8.0"]["has_fix"])
        self.assertEqual("6.8.0-11.11", by_series["6.8.0"]["first"])
        self.assertEqual("6.8.0-147.147", by_series["6.8.0"]["last"])
        self.assertFalse(by_series["7.0.0"]["has_fix"])
        self.assertEqual("critical", by_series["6.8.0"]["severity"])

    def test_classify_fix_available_below_fix(self):
        grouped = kernel.group(kernel.build_entries(self.record, {"linux"}))
        matches = kernel.match_entries(grouped, "linux", "6.8.0-142.142")
        self.assertEqual([("UBUNTU-CVE-1", "fix_available")],
                         [(entry["cve"], state) for entry, state in matches])

    def test_classify_patched_at_fix(self):
        grouped = kernel.group(kernel.build_entries(self.record, {"linux"}))
        self.assertEqual([], kernel.match_entries(grouped, "linux", "6.8.0-149.149"))

    def test_classify_no_fix_yet_for_open_series(self):
        grouped = kernel.group(kernel.build_entries(self.record, {"linux"}))
        matches = kernel.match_entries(grouped, "linux", "7.0.0-43.43~24.04.1")
        self.assertEqual([("UBUNTU-CVE-1", "no_fix_yet")],
                         [(entry["cve"], state) for entry, state in matches])

    def test_classify_older_than_first_affected(self):
        grouped = kernel.group(kernel.build_entries(self.record, {"linux"}))
        self.assertEqual([], kernel.match_entries(grouped, "linux", "6.8.0-5.5"))

    def test_other_series_is_not_matched(self):
        grouped = kernel.group(kernel.build_entries(self.record, {"linux"}))
        self.assertEqual([], kernel.match_entries(grouped, "linux", "6.11.0-1.1"))

    def test_fix_available_wins_over_no_fix_yet(self):
        # Range-mode entries for the same series: the open-ended one merges
        # into the one that knows the fix.
        record = _kernel_record(
            "UBUNTU-CVE-2",
            [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "linux"},
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "6.8.0-1.1"}]}
                    ],
                },
                {
                    "package": {"ecosystem": "Ubuntu", "name": "linux"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "6.8.0-1.1"},
                                {"fixed": "6.8.0-149.149"},
                            ],
                        }
                    ],
                },
            ],
        )
        grouped = kernel.group(kernel.build_entries(record, {"linux"}))
        matches = kernel.match_entries(grouped, "linux", "6.8.0-142.142")
        self.assertEqual(["fix_available"], [state for _, state in matches])

    def test_index_roundtrip(self):
        entries = kernel.build_entries(self.record, {"linux"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kernel-index.json")
            count = kernel.dump_index(path, "Ubuntu", ["linux"], entries)
            self.assertEqual(len(entries), count)
            ecosystem, grouped = kernel.load_index(path)
        self.assertEqual("Ubuntu", ecosystem)
        self.assertEqual(len(entries), sum(len(items) for items in grouped.values()))


class KernelCliTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-kernel-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        _write_record(
            self.db,
            "Ubuntu",
            _kernel_record(
                "UBUNTU-CVE-1",
                [
                    _linux_entry(
                        ["6.8.0-142.142"],
                        [{"introduced": "0"}, {"fixed": "6.8.0-149.149"}],
                    )
                ],
            ),
        )
        self.index = os.path.join(self.db, "kernel-index.json")
        self.acks = os.path.join(self.db, "acks.json")

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--db-dir", self.db, "--ack-db", self.acks] + argv
            )
        return code, out.getvalue()

    def test_index_and_match_pending(self):
        code, out = self._run(
            ["kernel-index", "--ecosystem", "Ubuntu", "--package", "linux", "--out", self.index]
        )
        self.assertEqual(0, code)
        self.assertIn("entries=1", out)
        code, out = self._run(
            [
                "kernel-match",
                "--index",
                self.index,
                "--pkg",
                "linux=6.8.0-142.142",
                "--format",
                "json",
            ]
        )
        self.assertEqual(0, code)
        findings = json.loads(out)["findings"]
        self.assertEqual(1, len(findings))
        self.assertEqual("fix_available", findings[0]["state"])
        self.assertEqual("kernel", findings[0]["category"])

    def test_match_patched_version_is_quiet(self):
        self._run(
            ["kernel-index", "--ecosystem", "Ubuntu", "--package", "linux", "--out", self.index]
        )
        code, out = self._run(
            [
                "kernel-match",
                "--index",
                self.index,
                "--pkg",
                "linux=6.8.0-149.149",
                "--format",
                "json",
            ]
        )
        self.assertEqual(0, code)
        self.assertEqual([], json.loads(out)["findings"])

    def test_prometheus_emits_count_metric(self):
        self._run(
            ["kernel-index", "--ecosystem", "Ubuntu", "--package", "linux", "--out", self.index]
        )
        code, out = self._run(
            [
                "kernel-match",
                "--index",
                self.index,
                "--pkg",
                "linux=6.8.0-142.142",
                "--format",
                "prometheus",
            ]
        )
        self.assertEqual(0, code)
        self.assertIn("osv_feed_kernel_cve_count", out)
        self.assertIn('cve_severity="critical"', out)
        self.assertIn('state="fix_available"', out)
        self.assertIn("} 1", out)

    def test_index_sourced_category_is_not_matched_from_the_mirror(self):
        config = {
            "tracking": {
                "perimeter": {"ecosystem": "Ubuntu", "packages": ["openssh"]},
                "kernel": {
                    "ecosystem": "Ubuntu",
                    "packages": ["linux=6.8.0-142.142"],
                    "source": "index",
                },
            }
        }
        class _Installed:
            def __init__(self):
                self.name = "openssh"
                self.version = "1:9.6p1-3"

        specs = cli._all_category_specs(config, "Ubuntu", lambda name: _Installed())
        self.assertEqual(["openssh"], [spec["package"] for spec in specs])


if __name__ == "__main__":
    unittest.main()
