import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from osv_feed import cli
from osv_feed import container
from osv_feed import matching
from osv_feed import mirror


def _write_record(db_dir, ecosystem, record):
    with mirror.connect(db_dir) as conn:
        mirror._insert_record(conn, ecosystem, record)


def _no_fix_record(record_id, ecosystem, package, version="1.0.0"):
    return {
        "id": record_id,
        "summary": f"test {record_id}",
        "affected": [
            {
                "package": {"ecosystem": ecosystem, "name": package},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}
                ],
            }
        ],
        "severity": [
            {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}
        ],
    }


class LoadManifestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="osv-container-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_parses_specs_ignoring_comments_and_blanks(self):
        path = os.path.join(self.dir, "webapp.manifest")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "# webapp 0.11.0\n"
                "PyPI:requests=2.32.3\n"
                "\n"
                "npm:express=4.19.2   # trailing comment\n"
                "Go:github.com/example/lib=1.2.3\n"
            )
        self.assertEqual(
            [
                {"ecosystem": "PyPI", "package": "requests", "version": "2.32.3"},
                {"ecosystem": "npm", "package": "express", "version": "4.19.2"},
                {
                    "ecosystem": "Go",
                    "package": "github.com/example/lib",
                    "version": "1.2.3",
                },
            ],
            container.load_manifest(path),
        )

    def test_image_name_from_basename(self):
        path = os.path.join(self.dir, "webapp-2.1.0-0.manifest")
        self.assertEqual("webapp-2.1.0-0", container.image_name(path))

    def test_missing_ecosystem_or_version_raises(self):
        path = os.path.join(self.dir, "bad.manifest")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("PyPI:requests=2.32.3\nrequests=2.32.3\n")
        with self.assertRaises(container.ManifestError):
            container.load_manifest(path)

    def test_load_manifests_tags_image(self):
        path = os.path.join(self.dir, "webapp.manifest")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("PyPI:requests=2.32.3\n")
        specs = container.load_manifests(self.dir)
        self.assertEqual(1, len(specs))
        self.assertEqual("webapp", specs[0]["image"])
        self.assertEqual("requests", specs[0]["package"])

    def test_empty_dir_returns_empty(self):
        self.assertEqual([], container.load_manifests(self.dir))


class MatchContainerTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-db-")
        self.manifest_dir = tempfile.mkdtemp(prefix="osv-manifests-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.manifest_dir, ignore_errors=True)

    def _manifest(self, name, lines):
        path = os.path.join(self.manifest_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_matches_manifest_with_image_label(self):
        _write_record(
            self.db,
            "PyPI",
            _no_fix_record("PYSEC-1", "PyPI", "requests", "2.32.3"),
        )
        self._manifest(
            "webapp.manifest",
            ["PyPI:requests=2.32.3", "PyPI:flask=3.0.0"],
        )
        findings = matching.match_container(self.db, self.manifest_dir)
        self.assertEqual(1, len(findings))
        self.assertEqual("webapp", findings[0]["image"])
        self.assertEqual("requests", findings[0]["package"])
        self.assertEqual("PyPI", findings[0]["ecosystem"])
        self.assertEqual("no_fix_yet", findings[0]["state"])

    def test_first_seen_is_per_image(self):
        _write_record(
            self.db,
            "PyPI",
            _no_fix_record("PYSEC-1", "PyPI", "requests", "2.32.3"),
        )
        self._manifest("webapp.manifest", ["PyPI:requests=2.32.3"])
        self._manifest("worker.manifest", ["PyPI:requests=2.32.3"])
        findings = matching.match_container(self.db, self.manifest_dir)
        first_seen = matching.first_seen_for(self.db, findings)
        keys = {tuple(k.split("|")[0:2]) for k in first_seen}
        self.assertEqual({("image", "webapp"), ("image", "worker")}, keys)
        self.assertEqual(2, len(first_seen))

    def test_empty_manifest_dir(self):
        self.assertEqual([], matching.match_container(self.db, self.manifest_dir))


class ContainerCliTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-cli-db-")
        self.manifest_dir = tempfile.mkdtemp(prefix="osv-cli-manifests-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.manifest_dir, ignore_errors=True)
        # The default no-flag run also auto-matches every host category, so
        # stub the feed-health check to isolate the container path under test.
        self._feed_patcher = mock.patch(
            "osv_feed.cli.feed_status", return_value="ok"
        )
        self._feed_patcher.start()
        self.addCleanup(self._feed_patcher.stop)

    def _write_manifest(self, name, lines):
        path = os.path.join(self.manifest_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _run(self, argv):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db-dir", self.db] + argv)
        return code, out.getvalue()

    def test_prometheus_includes_image_label_and_category(self):
        _write_record(
            self.db,
            "PyPI",
            _no_fix_record("PYSEC-1", "PyPI", "requests", "2.32.3"),
        )
        self._write_manifest("webapp.manifest", ["PyPI:requests=2.32.3"])
        code, out = self._run(
            ["match", "--container-dir", self.manifest_dir, "--format", "prometheus"]
        )
        self.assertEqual(0, code)
        self.assertIn(
            'osv_feed_cve_info{ecosystem="PyPI",package="requests",'
            'version="2.32.3",cve="PYSEC-1",severity="critical",'
            'state="no_fix_yet",category="container",acked="false",'
            'image="webapp"} 1',
            out,
        )
        # first-seen key is per-image and carries the image label.
        self.assertIn(
            'osv_feed_cve_first_seen_seconds{ecosystem="PyPI",package="requests",'
            'version="2.32.3",cve="PYSEC-1",image="webapp"}',
            out,
        )

    def test_container_category_filters_by_severity_default(self):
        # Low-severity container finding is dropped by the built-in high policy.
        record = {
            "id": "PYSEC-LOW",
            "summary": "low",
            "affected": [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "openssh"},
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}
                    ],
                }
            ],
            "severity": [
                {
                    "type": "CVSS_V3",
                    "score": "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:L/A:L",
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        self._write_manifest("weblib.manifest", ["Ubuntu:openssh=1.0"])
        code, out = self._run(
            ["match", "--container-dir", self.manifest_dir, "--format", "json"]
        )
        self.assertEqual(0, code)
        payload = json.loads(out)
        # The low-severity container finding is dropped by the built-in high
        # policy; any host auto-category findings are unrelated.
        self.assertEqual(
            [], [f for f in payload["findings"] if f["category"] == "container"]
        )

    def test_category_scopes_container_dirs(self):
        _write_record(
            self.db,
            "PyPI",
            _no_fix_record("PYSEC-1", "PyPI", "requests", "2.32.3"),
        )
        self._write_manifest("webapp.manifest", ["PyPI:requests=2.32.3"])

        # A non-container --category must not pull in container findings.
        code, out = self._run(
            [
                "match",
                "--category",
                "perimeter",
                "--container-dir",
                self.manifest_dir,
                "--format",
                "json",
            ]
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [],
            [f for f in json.loads(out)["findings"] if f["category"] == "container"],
        )

        # Explicit --category container yields only the container finding.
        code, out = self._run(
            [
                "match",
                "--category",
                "container",
                "--container-dir",
                self.manifest_dir,
                "--format",
                "json",
            ]
        )
        self.assertEqual(0, code)
        findings = json.loads(out)["findings"]
        self.assertEqual(["PYSEC-1"], [f["cve"] for f in findings])
        self.assertTrue(all(f["category"] == "container" for f in findings))
        self.assertEqual("webapp", findings[0]["image"])
