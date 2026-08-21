import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from osv_feed import cli
from osv_feed import matching
from osv_feed import mirror


def _write_record(db_dir, ecosystem, record):
    with mirror.connect(db_dir) as conn:
        mirror._insert_record(conn, ecosystem, record)


HIGH_CVSS = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
MEDIUM_CVSS = "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:H/A:H"
LOW_CVSS = "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:L/A:L"


def _no_fix_record(record_id, package, severity=None, summary=None):
    record = {
        "id": record_id,
        "summary": summary if summary is not None else f"test {record_id}",
        "affected": [
            {
                "package": {"ecosystem": "Ubuntu", "name": package},
                "ranges": [
                    {
                        "type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}],
                    }
                ],
            }
        ],
    }
    if severity:
        record["severity"] = [{"type": "CVSS_V3", "score": severity}]
    return record


class FormatAckedAtTest(unittest.TestCase):
    def test_format_acked_at_compacts_utc_timestamp(self):
        self.assertEqual(
            cli._format_acked_at("2026-08-16T09:24:24.966674+00:00"),
            "2026-08-16 09:24 UTC",
        )

    def test_format_acked_at_handles_z_suffix(self):
        self.assertEqual(cli._format_acked_at("2026-08-16T09:24:24Z"), "2026-08-16 09:24 UTC")

    def test_format_acked_at_empty(self):
        self.assertEqual(cli._format_acked_at(""), "")

    def test_format_acked_at_falls_back_on_unparseable(self):
        self.assertEqual(cli._format_acked_at("not-a-date"), "not-a-date")


class CliMatchTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-cli-")
        _write_record(self.db, "Ubuntu", _no_fix_record("UBUNTU-CVE-CLI", "openssh", HIGH_CVSS))
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db-dir", self.db] + argv)
        return code, out.getvalue()

    def test_db_immutable_flag_flows_to_match(self):
        local_state = tempfile.mkdtemp(prefix="osv-local-")
        self.addCleanup(shutil.rmtree, local_state, ignore_errors=True)
        with mock.patch("osv_feed.cli.feed_status", return_value="ok"), mock.patch(
            "osv_feed.cli.match_specs", return_value=[]
        ) as match_specs, mock.patch(
            "osv_feed.cli.default_db_dir", return_value=local_state
        ):
            code, _ = self._run(
                ["--db-immutable", "match", "--pkg", "openssh=1.0", "--format", "json"]
            )
        self.assertEqual(0, code)
        self.assertTrue(match_specs.call_args.kwargs["immutable"])

    def test_db_immutable_default_state_dir_is_local_not_snapshot(self):
        local_state = tempfile.mkdtemp(prefix="osv-local-")
        self.addCleanup(shutil.rmtree, local_state, ignore_errors=True)
        with mock.patch("osv_feed.cli.default_db_dir", return_value=local_state):
            code, _ = self._run(
                [
                    "--db-immutable",
                    "match",
                    "--pkg",
                    "openssh=1:9.6p1-3ubuntu13.3",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, code)
        self.assertTrue(os.path.exists(os.path.join(local_state, "first-seen.json")))
        self.assertFalse(os.path.exists(os.path.join(self.db, "first-seen.json")))

    def test_db_immutable_state_dir_override_stays_off_snapshot(self):
        state_dir = tempfile.mkdtemp(prefix="osv-state-")
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        code, _ = self._run(
            [
                "--db-immutable",
                "--state-dir",
                state_dir,
                "match",
                "--pkg",
                "openssh=1:9.6p1-3ubuntu13.3",
                "--format",
                "json",
            ]
        )
        self.assertEqual(0, code)
        self.assertTrue(os.path.exists(os.path.join(state_dir, "first-seen.json")))
        self.assertFalse(os.path.exists(os.path.join(self.db, "first-seen.json")))

    def test_json_output(self):
        code, output = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "json"]
        )
        self.assertEqual(0, code)
        data = json.loads(output)
        self.assertEqual("no_fix_yet", data["findings"][0]["state"])
        self.assertEqual("perimeter", data["findings"][0]["category"])
        self.assertIn("severity", data["findings"][0])

    def test_table_includes_severity_column(self):
        code, output = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "table"]
        )
        self.assertEqual(0, code)
        self.assertIn("SEVERITY", output)

    def test_prometheus_output(self):
        code, output = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "prometheus"]
        )
        self.assertEqual(0, code)
        self.assertIn('state="no_fix_yet"', output)
        self.assertIn('category="perimeter"', output)
        self.assertIn("osv_feed_cve_first_seen_seconds", output)
        self.assertIn("osv_feed_cve_epss_score", output)
        self.assertIn("osv_feed_cve_epss_percentile", output)
        self.assertIn("osv_feed_cve_summary", output)

    def test_cve_summary_coalesces_pending_past_sla(self):
        now = int(time.time())
        base = {"ecosystem": "Ubuntu", "requested_package": "nginx", "version": "1.0"}
        findings = [
            {**base, "cve": "CVE-C1", "severity": "critical", "state": "no_fix_yet", "acked": False},
            {**base, "cve": "CVE-C2", "severity": "critical", "state": "no_fix_yet", "acked": False},
            {**base, "cve": "CVE-H1", "severity": "high", "state": "no_fix_yet", "acked": False},
            {**base, "cve": "CVE-ACK", "severity": "critical", "state": "no_fix_yet", "acked": True},
            {**base, "cve": "CVE-NEW", "severity": "critical", "state": "no_fix_yet", "acked": False},
            {**base, "cve": "CVE-PT", "severity": "critical", "state": "patched", "acked": False},
        ]
        first_seen = {
            "Ubuntu|nginx|1.0|CVE-C1": now - 20 * 3600,
            "Ubuntu|nginx|1.0|CVE-C2": now - 13 * 3600,
            "Ubuntu|nginx|1.0|CVE-H1": now - 3 * 24 * 3600,
            "Ubuntu|nginx|1.0|CVE-NEW": now - 60,
            "Ubuntu|nginx|1.0|CVE-ACK": now - 20 * 3600,
            "Ubuntu|nginx|1.0|CVE-PT": now - 20 * 3600,
        }
        summary = cli._cve_summary(findings, first_seen)
        self.assertEqual({"count": 2, "cves": ["CVE-C1", "CVE-C2"]}, summary["critical"])
        self.assertEqual({"count": 1, "cves": ["CVE-H1"]}, summary["high"])

    def test_cve_summary_lists_top_five_oldest_first(self):
        now = int(time.time())
        base = {
            "ecosystem": "Ubuntu",
            "requested_package": "nginx",
            "version": "1.0",
            "severity": "high",
            "state": "no_fix_yet",
            "acked": False,
        }
        findings = [{**base, "cve": f"CVE-H{i}"} for i in range(7)]
        first_seen = {f"Ubuntu|nginx|1.0|CVE-H{i}": now - (2 * 24 * 3600 + i) for i in range(7)}
        summary = cli._cve_summary(findings, first_seen)
        self.assertEqual(7, summary["high"]["count"])
        # Oldest first_seen first; larger i has a smaller (older) epoch.
        self.assertEqual(
            ["CVE-H6", "CVE-H5", "CVE-H4", "CVE-H3", "CVE-H2"],
            summary["high"]["cves"],
        )

    def test_prometheus_render_emits_coalesced_summary(self):
        now = int(time.time())
        base = {"ecosystem": "Ubuntu", "requested_package": "nginx", "version": "1.0"}
        findings = [
            {
                **base,
                "cve": "CVE-C1",
                "severity": "critical",
                "state": "no_fix_yet",
                "acked": False,
            },
            {
                **base,
                "cve": "CVE-H\"1",
                "severity": "high",
                "state": "no_fix_yet",
                "acked": False,
            },
        ]
        first_seen = {
            "Ubuntu|nginx|1.0|CVE-C1": now - 20 * 3600,
            'Ubuntu|nginx|1.0|CVE-H"1': now - 3 * 24 * 3600,
        }
        output = cli._render_prometheus(findings, first_seen)
        self.assertIn('osv_feed_cve_summary{severity="critical",acked="false",cves="CVE-C1"} 1', output)
        self.assertIn(
            'osv_feed_cve_summary{severity="high",acked="false",cves="CVE-H\\"1"} 1', output
        )
        self.assertNotIn('severity="medium"', output)

    def test_empty_findings_still_exit_zero(self):
        code, output = self._run(
            ["match", "--pkg", "openssh-server=1:9.6p1-3ubuntu13.3", "--format", "json"]
        )
        self.assertEqual(0, code)
        self.assertEqual([], json.loads(output)["findings"])

    def test_spec_file(self):
        spec = [
            {"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}
        ]
        with open(os.path.join(self.db, "specs.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        code, output = self._run(
            ["match", "--spec-file", os.path.join(self.db, "specs.json"), "--format", "json"]
        )
        self.assertEqual(0, code)
        data = json.loads(output)
        self.assertEqual("no_fix_yet", data["findings"][0]["state"])
        self.assertEqual("perimeter", data["findings"][0]["category"])

    @mock.patch(
        "osv_feed.cli.installed_package",
        side_effect=lambda name: SimpleNamespace(name=name, version="1:9.6p1-3ubuntu13.3"),
    )
    def test_no_pkg_matches_all_category_packages(self, installed):
        code, output = self._run(["match", "--format", "json"])
        self.assertEqual(0, code)
        data = json.loads(output)
        self.assertIn("UBUNTU-CVE-CLI", [f["cve"] for f in data["findings"]])
        self.assertEqual(["perimeter"], [f["category"] for f in data["findings"]])


class CliInstalledVersionTest(unittest.TestCase):
    VERSION = "1:9.6p1-3ubuntu13.3"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-resolve-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    @staticmethod
    def _specs(pkg=None, spec_file=None, resolver=None):
        args = argparse.Namespace(ecosystem="Ubuntu", pkg=pkg, spec_file=spec_file)
        return cli._parse_specs(args, resolver=resolver)

    def test_pkg_without_version_uses_installed(self):
        def resolver(name):
            self.assertEqual("openssh-client", name)
            return SimpleNamespace(name="openssh", version=self.VERSION)

        specs = self._specs(pkg=["openssh-client"], resolver=resolver)
        self.assertEqual(
            [{"ecosystem": "Ubuntu", "package": "openssh", "version": self.VERSION}],
            specs,
        )

    def test_explicit_version_does_not_call_resolver(self):
        def resolver(name):
            raise AssertionError(f"resolver should not be called for {name!r}")

        specs = self._specs(pkg=[f"openssh={self.VERSION}"], resolver=resolver)
        self.assertEqual(
            [{"ecosystem": "Ubuntu", "package": "openssh", "version": self.VERSION}],
            specs,
        )

    def test_spec_file_version_optional(self):
        spec_file = os.path.join(self.tmp, "specs.json")
        with open(spec_file, "w", encoding="utf-8") as fh:
            json.dump({"ecosystem": "Ubuntu", "package": "openssh-client"}, fh)

        def resolver(name):
            self.assertEqual("openssh-client", name)
            return SimpleNamespace(name="openssh", version=self.VERSION)

        specs = self._specs(spec_file=spec_file, resolver=resolver)
        self.assertEqual(
            [{"ecosystem": "Ubuntu", "package": "openssh", "version": self.VERSION}],
            specs,
        )

    def test_unresolvable_version_warns_and_skips(self):
        def resolver(name):
            raise LookupError(f"{name!r} is not installed and no version was given")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            specs = self._specs(pkg=["openssh"], resolver=resolver)
        self.assertEqual([], specs)
        self.assertIn("warning", err.getvalue())
        self.assertIn("not installed", err.getvalue())

    @mock.patch(
        "osv_feed.cli.installed_package",
        return_value=SimpleNamespace(name="openssh", version="1:9.6p1-3ubuntu13.3"),
    )
    def test_main_resolves_installed_source_package(self, installed):
        db = tempfile.mkdtemp(prefix="osv-cli-installed-")
        self.addCleanup(shutil.rmtree, db, ignore_errors=True)
        _write_record(db, "Ubuntu", _no_fix_record("UBUNTU-CVE-CLI", "openssh", HIGH_CVSS))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--db-dir", db, "match", "--pkg", "openssh-client", "--format", "json"]
            )
        self.assertEqual(0, code)
        installed.assert_called_once_with("openssh-client")
        data = json.loads(out.getvalue())
        self.assertEqual("no_fix_yet", data["findings"][0]["state"])
        self.assertEqual("perimeter", data["findings"][0]["category"])
        self.assertEqual(self.VERSION, data["findings"][0]["version"])


class CliFreshnessWarningTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-warn-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    def _run(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db-dir", self.db] + argv)
        return code, out.getvalue(), err.getvalue()

    @mock.patch("osv_feed.cli.feed_status", return_value="absent")
    def test_absent_feed_warns_and_exits_nonzero(self, status):
        code, out, err = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "json"]
        )
        self.assertEqual(1, code)
        self.assertEqual("", out)
        status.assert_called_once_with(self.db, "Ubuntu")
        self.assertIn("Ubuntu feed is absent", err)
        self.assertIn("osv-feed mirror --ecosystem Ubuntu", err)

    @mock.patch("osv_feed.cli.feed_status", return_value="stale")
    def test_stale_feed_warns(self, status):
        code, _, err = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "json"]
        )
        self.assertEqual(0, code)
        self.assertIn("Ubuntu feed is more than a day old", err)

    @mock.patch("osv_feed.cli.feed_status", return_value="ok")
    def test_fresh_feed_is_quiet(self, status):
        code, _, err = self._run(
            ["match", "--pkg", "openssh=1:9.6p1-3ubuntu13.3", "--format", "json"]
        )
        self.assertEqual(0, code)
        self.assertEqual("", err)


class CliEcosystemSelectionTest(unittest.TestCase):
    def setUp(self):
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    def test_explicit_ecosystem_not_merged_with_default(self):
        parser = cli.build_parser()
        args = parser.parse_args(["mirror", "--ecosystem", "Debian"])
        self.assertEqual(["Debian"], cli._mirror_ecosystems(args))

    def test_defaults_to_host_ecosystem_when_omitted(self):
        parser = cli.build_parser()
        args = parser.parse_args(["mirror"])
        self.assertIsNone(args.ecosystem)
        # host_ecosystem is patched to Ubuntu in setUp.
        self.assertEqual(["Ubuntu"], cli._mirror_ecosystems(args))

    def test_multiple_ecosystems_preserved(self):
        parser = cli.build_parser()
        args = parser.parse_args(["mirror", "--ecosystem", "Ubuntu", "--ecosystem", "Debian"])
        self.assertEqual(["Ubuntu", "Debian"], cli._mirror_ecosystems(args))

    @mock.patch("osv_feed.cli.host_ecosystem", return_value=None)
    def test_no_default_when_host_is_not_debian_or_ubuntu(self, _host):
        parser = cli.build_parser()
        args = parser.parse_args(["mirror"])
        with self.assertRaises(SystemExit):
            cli._mirror_ecosystems(args)

    def test_parse_specs_resolves_host_ecosystem(self):
        args = argparse.Namespace(
            ecosystem=None, pkg=["openssh=1:9.6p1-3ubuntu13.3"], spec_file=None
        )
        specs = cli._parse_specs(args)
        self.assertEqual("Ubuntu", specs[0]["ecosystem"])
        self.assertEqual("openssh", specs[0]["package"])

    @mock.patch("osv_feed.cli.host_ecosystem", return_value=None)
    def test_parse_specs_requires_ecosystem_for_pkg_on_other_os(self, _host):
        args = argparse.Namespace(
            ecosystem=None, pkg=["openssh=1.0"], spec_file=None
        )
        with self.assertRaisesRegex(SystemExit, "cannot determine ecosystem"):
            cli._parse_specs(args)


class CliMirrorTest(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db-dir", self.db] + argv)
        return code, out.getvalue()

    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-mirror-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    @mock.patch("osv_feed.cli.mirror_epss", return_value=42)
    @mock.patch(
        "osv_feed.cli.mirror_ecosystem",
        return_value={"downloaded": 5, "removed": 0},
    )
    def test_mirror_also_mirrors_epss(self, _eco, mirror_epss):
        code, out = self._run(["mirror"])
        self.assertEqual(0, code)
        self.assertIn("Ubuntu: downloaded=5", out)
        self.assertIn("epss: downloaded=42", out)
        mirror_epss.assert_called_once_with(self.db, force=False, progress=mock.ANY)

    @mock.patch("osv_feed.cli.mirror_epss", return_value=-1)
    @mock.patch(
        "osv_feed.cli.mirror_ecosystem",
        return_value={"downloaded": 5, "removed": 0},
    )
    def test_mirror_prints_epss_up_to_date_when_skipped(self, _eco, mirror_epss):
        code, out = self._run(["mirror"])
        self.assertEqual(0, code)
        self.assertIn("epss: up to date", out)


class CliCategoryAckTest(unittest.TestCase):
    CONFIG = {
        "tracking": {
            "perimeter": {
                "ecosystem": "Debian",
                "packages": ["openssh-client", "nginx"],
                "severity": "high",
                "states": ["no_fix_yet", "fix_available"],
                "interests": ["rce", "remote code execution"],
            },
            "user": {
                "packages": ["sudo"],
                "severity": "high",
                "interests": ["privilege escalation"],
            },
        }
    }

    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-category-")
        self.ack_db = os.path.join(self.db, "acks.json")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        records = [
            {
                "id": "DEBIAN-CVE-RCE",
                "summary": "RCE in sshd",
                "details": "remote code execution in sshd before an upcoming release.",
                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                "affected": [
                    {
                        "package": {"ecosystem": "Debian", "name": "openssh"},
                        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                    }
                ],
            },
            {
                "id": "DEBIAN-CVE-PRIVESC",
                "summary": "privilege escalation",
                "details": "local privilege escalation in sudo.",
                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                "affected": [
                    {
                        "package": {"ecosystem": "Debian", "name": "openssh"},
                        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                    }
                ],
            },
        ]
        for record in records:
            _write_record(self.db, "Debian", record)

    def _match(self, category=None):
        argv = [
            "--db-dir", self.db,
            "--ack-db", self.ack_db,
            "match", "--ecosystem", "Debian", "--format", "json",
        ]
        if category:
            argv.append("--category")
            argv.append(category)
        else:
            argv.extend(["--pkg", "openssh=1:9.6p1-3ubuntu13.3"])
        out = io.StringIO()
        installed = mock.patch(
            "osv_feed.cli.installed_package",
            side_effect=lambda name: SimpleNamespace(
                name="openssh" if name == "openssh-client" else name,
                version="1:9.6p1-3ubuntu13.3",
            ),
        )
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG), mock.patch(
            "osv_feed.cli.feed_status", return_value="ok"
        ), installed:
            with contextlib.redirect_stdout(out):
                code = cli.main(argv)
        return code, json.loads(out.getvalue())

    def test_category_packages_and_filters(self):
        code, data = self._match(category="perimeter")
        self.assertEqual(0, code)
        self.assertEqual(["DEBIAN-CVE-RCE"], [f["cve"] for f in data["findings"]])
        self.assertEqual("perimeter", data["findings"][0]["category"])
        self.assertEqual("no_fix_yet", data["findings"][0]["state"])

    def test_ack_suppresses_alert_label_but_keeps_finding(self):
        self._match(category="perimeter")
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--db-dir", self.db,
                        "--ack-db", self.ack_db,
                        "ack", "add",
                        "--category", "perimeter",
                        "--ecosystem", "Debian",
                        "--cve", "DEBIAN-CVE-RCE",
                        "--note", "blocked at firewall",
                    ]
                )
        self.assertEqual(0, code, out.getvalue())

        code, data = self._match(category="perimeter")
        self.assertEqual(0, code)
        finding = data["findings"][0]
        self.assertTrue(finding["acked"])
        self.assertEqual("blocked at firewall", finding.get("ack_note"))
        self.assertIn("acked_at", finding)

    def test_ack_without_ecosystem_matches_by_wildcard(self):
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--db-dir", self.db,
                        "--ack-db", self.ack_db,
                        "ack", "add",
                        "--category", "perimeter",
                        "--cve", "DEBIAN-CVE-RCE",
                    ]
                )
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("any ecosystem", out.getvalue())

        code, data = self._match(category="perimeter")
        self.assertEqual(0, code)
        finding = data["findings"][0]
        self.assertTrue(finding["acked"])
        self.assertIn("acked_at", finding)

    def test_prometheus_includes_category_and_acked_labels(self):
        argv = [
            "--db-dir", self.db,
            "--ack-db", self.ack_db,
            "match", "--category", "perimeter", "--format", "prometheus",
        ]
        out = io.StringIO()
        installed = mock.patch(
            "osv_feed.cli.installed_package",
            return_value=SimpleNamespace(name="openssh", version="1:9.6p1-3ubuntu13.3"),
        )
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG), mock.patch(
            "osv_feed.cli.feed_status", return_value="ok"
        ), installed:
            with contextlib.redirect_stdout(out):
                code = cli.main(argv)
        self.assertEqual(0, code)
        self.assertIn('category="perimeter"', out.getvalue())
        self.assertIn('acked="false"', out.getvalue())

    def test_ack_list_json(self):
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            code = cli.main(
                [
                    "--ack-db", self.ack_db,
                    "ack", "add",
                    "--category", "user",
                    "--ecosystem", "Ubuntu",
                    "--cve", "UBUNTU-CVE-1",
                ]
            )
        self.assertEqual(0, code)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--ack-db", self.ack_db, "ack", "list", "--format", "json"]
            )
        self.assertEqual(0, code)
        entries = json.loads(out.getvalue())
        self.assertEqual(["UBUNTU-CVE-1"], [entry["cve"] for entry in entries])

    def test_remove_ack(self):
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            cli.main(
                [
                    "--ack-db", self.ack_db,
                    "ack", "add",
                    "--category", "user",
                    "--ecosystem", "Ubuntu",
                    "--cve", "UBUNTU-CVE-1",
                ]
            )
            code = cli.main(
                [
                    "--ack-db", self.ack_db,
                    "ack", "remove",
                    "--category", "user",
                    "--ecosystem", "Ubuntu",
                    "--cve", "UBUNTU-CVE-1",
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual([], cli.list_acks(self.ack_db))

    def test_ack_surface_scoped_does_not_match_surfaceless_finding(self):
        # A surface-scoped ack must not silence the same CVE on a host
        # (surface-less) finding, so shared CVEs stay per-surface controllable.
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--db-dir", self.db,
                        "--ack-db", self.ack_db,
                        "ack", "add",
                        "--category", "perimeter",
                        "--ecosystem", "Debian",
                        "--cve", "DEBIAN-CVE-RCE",
                        "--surface", "authentik",
                    ]
                )
        self.assertEqual(0, code, out.getvalue())
        self.assertIn("surface 'authentik'", out.getvalue())

        code, data = self._match(category="perimeter")
        self.assertEqual(0, code)
        finding = data["findings"][0]
        self.assertFalse(finding["acked"], "surface-scoped ack leaked to a host finding")

    def test_ack_list_filters_by_surface(self):
        with mock.patch("osv_feed.cli.load_config", return_value=self.CONFIG):
            cli.main(
                [
                    "--ack-db", self.ack_db, "ack", "add",
                    "--category", "container", "--ecosystem", "PyPI",
                    "--cve", "GHSA-1", "--surface", "open-webui",
                ]
            )
            cli.main(
                [
                    "--ack-db", self.ack_db, "ack", "add",
                    "--category", "container", "--ecosystem", "PyPI",
                    "--cve", "GHSA-2", "--surface", "zulip",
                ]
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--ack-db", self.ack_db, "ack", "list",
                        "--surface", "zulip", "--format", "json",
                    ]
                )
        self.assertEqual(0, code)
        entries = json.loads(out.getvalue())
        self.assertEqual(["GHSA-2"], [entry["cve"] for entry in entries])
        self.assertEqual("zulip", entries[0]["surface"])


class CliDefaultCategoryTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-default-")
        self.ack_db = os.path.join(self.db, "acks.json")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self._host_patcher = mock.patch("osv_feed.cli.host_ecosystem", return_value="Ubuntu")
        self._host_patcher.start()
        self.addCleanup(self._host_patcher.stop)

    def _run(self, argv, config=None):
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value=config or {}):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    ["--db-dir", self.db, "--ack-db", self.ack_db] + argv
                )
        return code, json.loads(out.getvalue())

    def test_uncategorized_package_uses_default_medium_category(self):
        _write_record(self.db, "Ubuntu", _no_fix_record("UBUNTU-CVE-MED", "dovecot", MEDIUM_CVSS))
        _write_record(self.db, "Ubuntu", _no_fix_record("UBUNTU-CVE-LOW", "dovecot", LOW_CVSS))
        code, data = self._run(
            ["match", "--pkg", "dovecot=1.2.3", "--format", "json"]
        )
        self.assertEqual(0, code)
        self.assertEqual(["default"], [f["category"] for f in data["findings"]])
        self.assertEqual(["UBUNTU-CVE-MED"], [f["cve"] for f in data["findings"]])
        self.assertEqual("no_fix_yet", data["findings"][0]["state"])

    def test_no_category_matches_all_categories_for_packages(self):
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record("UBUNTU-CVE-SSH", "openssh", HIGH_CVSS),
        )
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record(
                "UBUNTU-CVE-SUDO", "sudo", HIGH_CVSS,
                summary="local privilege escalation in sudo",
            ),
        )
        code, data = self._run(
            ["match",
             "--pkg", "openssh=1:9.6p1-3ubuntu13.3",
             "--pkg", "sudo=1:1.9.9",
             "--format", "json"]
        )
        self.assertEqual(0, code)
        cats = {(f["cve"], f["category"]) for f in data["findings"]}
        self.assertIn(("UBUNTU-CVE-SSH", "perimeter"), cats)
        self.assertIn(("UBUNTU-CVE-SUDO", "user"), cats)

    def test_config_category_detected_without_flag(self):
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record("UBUNTU-CVE-HAPROXY", "haproxy", HIGH_CVSS, summary="RCE in haproxy"),
        )
        config = {
            "tracking": {
                "edge": {"packages": ["haproxy"], "severity": "high", "interests": ["rce"]}
            }
        }
        code, data = self._run(
            ["match", "--pkg", "haproxy=2.6", "--format", "json"],
            config=config,
        )
        self.assertEqual(0, code)
        self.assertEqual(["edge"], [f["category"] for f in data["findings"]])

    def test_verbose_table_shows_title_interests_and_references(self):
        record = {
            "id": "UBUNTU-CVE-VERBOSE",
            "summary": "RCE in haproxy",
            "details": "remote code execution in haproxy before release.",
            "aliases": ["CVE-2026-99999"],
            "references": [{"type": "ADVISORY", "url": "https://example.test/advisory"}],
            "severity": [{"type": "CVSS_V3", "score": HIGH_CVSS}],
            "affected": [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "haproxy"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        config = {
            "tracking": {
                "edge": {
                    "packages": ["haproxy"],
                    "severity": "high",
                    "interests": ["rce", "remote code execution"],
                }
            }
        }
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value=config):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--db-dir", self.db,
                        "--ack-db", self.ack_db,
                        "match", "--pkg", "haproxy=2.6", "-v", "--format", "table",
                    ]
                )
        self.assertEqual(0, code)
        text = out.getvalue()
        self.assertIn("RCE in haproxy", text)
        self.assertIn("interests:", text)
        self.assertIn("rce", text)
        self.assertIn("remote code execution", text)
        self.assertIn("CVE-2026-99999", text)
        self.assertIn("https://example.test/advisory", text)

    def test_json_includes_title_and_reference_details(self):
        record = {
            "id": "UBUNTU-CVE-DETAILS",
            "summary": "privilege escalation in sudo",
            "aliases": ["CVE-2026-11111"],
            "references": [{"type": "ADVISORY", "url": "https://example.test/adv"}],
            "severity": [{"type": "CVSS_V3", "score": HIGH_CVSS}],
            "affected": [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "sudo"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        code, data = self._run(
            ["match", "--pkg", "sudo=1:1.9.9", "--format", "json"]
        )
        self.assertEqual(0, code)
        finding = data["findings"][0]
        self.assertEqual("privilege escalation in sudo", finding["summary"])
        self.assertEqual(["CVE-2026-11111"], finding["aliases"])
        self.assertEqual(["https://example.test/adv"], finding["references"])

    def test_verbose_title_falls_back_to_details_when_summary_empty(self):
        record = {
            "id": "DEBIAN-CVE-NOTITLE",
            "summary": "",
            "details": (
                "A privilege escalation in sudo lets a local user gain root. "
                "More context follows here."
            ),
            "severity": [{"type": "CVSS_V3", "score": HIGH_CVSS}],
            "affected": [
                {
                    "package": {"ecosystem": "Debian", "name": "sudo"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                }
            ],
        }
        _write_record(self.db, "Debian", record)
        out = io.StringIO()
        with mock.patch("osv_feed.cli.load_config", return_value={}):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    [
                        "--db-dir", self.db,
                        "--ack-db", self.ack_db,
                        "match", "--ecosystem", "Debian", "-v",
                        "--pkg", "sudo=1:1.9.9", "--format", "table",
                    ]
                )
        self.assertEqual(0, code)
        self.assertIn(
            "title:       A privilege escalation in sudo lets a local user gain root",
            out.getvalue(),
        )

    @mock.patch(
        "osv_feed.cli.installed_package",
        side_effect=lambda name: SimpleNamespace(name=name, version="1.0"),
    )
    def test_no_args_matches_all_category_packages(self, installed):
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record("UBUNTU-CVE-SSH", "openssh", HIGH_CVSS),
        )
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record(
                "UBUNTU-CVE-SUDO", "sudo", HIGH_CVSS,
                summary="local privilege escalation in sudo",
            ),
        )
        code, data = self._run(["match", "--format", "json"])
        self.assertEqual(0, code)
        cats = {(f["cve"], f["category"]) for f in data["findings"]}
        self.assertIn(("UBUNTU-CVE-SSH", "perimeter"), cats)
        self.assertIn(("UBUNTU-CVE-SUDO", "user"), cats)

    def test_uninstalled_category_packages_are_reported_not_dropped(self):
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record("UBUNTU-CVE-SSH", "openssh", HIGH_CVSS),
        )

        def resolver(name):
            if name == "openssh":
                return SimpleNamespace(name="openssh", version="1.0")
            raise cli.PackageNotInstalledError(
                f"{name!r} is not installed and no version was given"
            )

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("osv_feed.cli.installed_package", side_effect=resolver):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(
                    ["--db-dir", self.db, "--ack-db", self.ack_db,
                     "match", "--format", "json"]
                )
        self.assertEqual(0, code)
        self.assertNotIn("not installed", err.getvalue())
        data = json.loads(out.getvalue())
        rows = {(f["cve"], f["category"], f["state"]) for f in data["findings"]}
        self.assertIn(("UBUNTU-CVE-SSH", "perimeter", "no_fix_yet"), rows)
        uninstalled = [f for f in data["findings"] if f["state"] == "uninstalled"]
        self.assertTrue(uninstalled)
        self.assertEqual("", uninstalled[0]["version"])
        self.assertEqual("", uninstalled[0]["cve"])

    def test_uninstalled_package_uses_installed_version_when_given(self):
        """An explicitly pinned version matches even if the host lacks it."""
        _write_record(
            self.db,
            "Ubuntu",
            _no_fix_record("UBUNTU-CVE-NGINX", "nginx", HIGH_CVSS),
        )

        out = io.StringIO()
        with mock.patch(
            "osv_feed.cli.installed_package",
            side_effect=cli.PackageNotInstalledError("not installed"),
        ):
            with contextlib.redirect_stdout(out):
                code = cli.main(
                    ["--db-dir", self.db, "--ack-db", self.ack_db,
                     "match", "--pkg", "nginx=1.0", "--format", "json"]
                )
        self.assertEqual(0, code)
        data = json.loads(out.getvalue())
        self.assertEqual(
            [("UBUNTU-CVE-NGINX", "1.0")],
            [(f["cve"], f["version"]) for f in data["findings"]],
        )


if __name__ == "__main__":
    unittest.main()
