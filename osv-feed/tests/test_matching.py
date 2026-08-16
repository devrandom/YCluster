import json
import os
import shutil
import tempfile
import unittest

from osv_feed import matching
from osv_feed import mirror


def _write_record(db_dir, ecosystem, record):
    with mirror.connect(db_dir) as conn:
        mirror._insert_record(conn, ecosystem, record)


def _basic_record(record_id, package, introduced="0", fixed=None, last_affected=None):
    events = [{"introduced": introduced}]
    if fixed:
        events.append({"fixed": fixed})
    if last_affected:
        events.append({"last_affected": last_affected})
    return {
        "id": record_id,
        "summary": f"test finding for {package}",
        "affected": [
            {
                "package": {"ecosystem": "Ubuntu", "name": package},
                "ranges": [{"type": "ECOSYSTEM", "events": events}],
            }
        ],
    }


class MatchSpecsTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-feed-test-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)

    def test_fix_available(self):
        _write_record(
            self.db,
            "Ubuntu",
            _basic_record("UBUNTU-CVE-FIX", "openssh", fixed="1:9.6p1-3ubuntu13.5"),
        )
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(1, len(findings))
        self.assertEqual("fix_available", findings[0]["state"])

    def test_patched_after_fixed(self):
        _write_record(
            self.db,
            "Ubuntu",
            _basic_record("UBUNTU-CVE-FIX", "openssh", fixed="1:9.6p1-3ubuntu13.5"),
        )
        for version in ("1:9.6p1-3ubuntu13.5", "1:9.7p1-1"):
            findings = matching.match_specs(
                self.db,
                [{"ecosystem": "Ubuntu", "package": "openssh", "version": version}],
            )
            self.assertEqual("patched", findings[0]["state"], version)

    def test_no_fix_yet(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("no_fix_yet", findings[0]["state"])

    def test_last_affected_bound(self):
        _write_record(
            self.db,
            "Ubuntu",
            _basic_record("UBUNTU-CVE-LAST", "openssh", introduced="1.0", last_affected="2.0"),
        )
        hit = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1.5"}]
        )
        miss = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "2.1"}]
        )
        self.assertEqual("no_fix_yet", hit[0]["state"])
        self.assertEqual("patched", miss[0]["state"])

    def test_versions_list(self):
        record = {
            "id": "PYSEC-TEST",
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": "demo"},
                    "versions": ["1.0.0"],
                }
            ],
        }
        _write_record(self.db, "PyPI", record)
        hit = matching.match_specs(
            self.db, [{"ecosystem": "PyPI", "package": "demo", "version": "1.0.0"}]
        )
        miss = matching.match_specs(
            self.db, [{"ecosystem": "PyPI", "package": "demo", "version": "1.0.1"}]
        )
        self.assertEqual("no_fix_yet", hit[0]["state"])
        self.assertEqual("patched", miss[0]["state"])

    def test_binary_name_does_not_match_source_package(self):
        _write_record(
            self.db,
            "Ubuntu",
            _basic_record("UBUNTU-CVE-SRC", "openssh"),
        )
        findings = matching.match_specs(
            self.db,
            [{"ecosystem": "Ubuntu", "package": "openssh-server", "version": "1:9.6p1-3ubuntu13.3"}],
        )
        self.assertEqual([], findings)

    def test_severity_prefers_debian_urgency(self):
        record = {
            "id": "DEBIAN-CVE-URGENCY",
            "affected": [
                {
                    "package": {"name": "openssh"},
                    "ecosystem_specific": {"urgency": "high"},
                    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N"}],
                }
            ],
        }
        _write_record(self.db, "Debian", record)
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Debian", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("high", findings[0]["severity"])

    def test_severity_uses_ubuntu_priority(self):
        record = {
            "id": "UBUNTU-CVE-PRIORITY",
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H"},
                {"type": "Ubuntu", "score": "high"},
            ],
            "affected": [{"package": {"name": "openssh"}}],
        }
        _write_record(self.db, "Ubuntu", record)
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("high", findings[0]["severity"])

    def test_severity_falls_back_to_cvss(self):
        record = {
            "id": "UBUNTU-CVE-CVSS",
            "affected": [
                {
                    "package": {"name": "openssh"},
                    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("critical", findings[0]["severity"])

    def test_severity_empty_when_unavailable(self):
        _write_record(self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOSEV", "openssh"))
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("", findings[0]["severity"])

    def test_severity_cvss4_uses_osv_database_label(self):
        # CVSS 4.0 has no stable vector-only qualitative rating; the
        # advisory's own ``database_specific.severity`` (GitHub rating on
        # GHSA/Go records) supplies the label.
        record = {
            "id": "GHSA-CVSS4",
            "database_specific": {"severity": "HIGH"},
            "severity": [
                {"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N"}
            ],
            "affected": [{"package": {"name": "github.com/fatedier/frp"}}],
        }
        _write_record(self.db, "Go", record)
        findings = matching.match_specs(
            self.db,
            [{"ecosystem": "Go", "package": "github.com/fatedier/frp", "version": "0.70.0"}],
        )
        self.assertEqual("high", findings[0]["severity"])

    def test_go_semver_frp_range_and_patch(self):
        record = {
            "id": "GHSA-26gq-p25f-99cp",
            "database_specific": {"severity": "HIGH"},
            "severity": [
                {"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N"}
            ],
            "affected": [
                {
                    "package": {"ecosystem": "Go", "name": "github.com/fatedier/frp"},
                    "ranges": [
                        {
                            "type": "SEMVER",
                            "events": [
                                {"introduced": "0.53.0"},
                                {"fixed": "0.70.1"},
                            ],
                        }
                    ],
                }
            ],
        }
        _write_record(self.db, "Go", record)
        cases = {
            "0.70.0": ("fix_available", "high"),   # deployed pin: inside range, fixed in 0.70.1
            "0.70.1": ("patched", "high"),          # at/after the fix
            "0.40.0": ("patched", "high"),          # before introduced
        }
        for version, (state, severity) in cases.items():
            findings = matching.match_specs(
                self.db,
                [{"ecosystem": "Go", "package": "github.com/fatedier/frp", "version": version}],
            )
            self.assertEqual(state, findings[0]["state"], version)
            self.assertEqual(severity, findings[0]["severity"], version)

    def test_candidate_records_uses_package_index(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-A", "openssh")
        )
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-B", "nginx")
        )
        records = matching._candidate_records(self.db, "Ubuntu", ["openssh"])
        self.assertEqual(["UBUNTU-CVE-A"], [record["id"] for record in records])

    def test_candidate_records_immutable_read(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-A", "openssh")
        )
        records = matching._candidate_records(
            self.db, "Ubuntu", ["openssh"], immutable=True
        )
        self.assertEqual(["UBUNTU-CVE-A"], [record["id"] for record in records])
        db = os.path.join(self.db, "osv-feed.sqlite")
        for sidecar in ("-journal", "-wal", "-shm"):
            self.assertFalse(
                os.path.exists(db + sidecar),
                f"immutable read must not create {db + sidecar}",
            )

    def test_first_seen_persists_and_stale_keys_dropped(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        specs = [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        findings_a = matching.match_specs(self.db, specs)
        findings_b = matching.match_specs(self.db, specs)
        first_seen_a = matching.first_seen_for(self.db, findings_a)
        first_seen_b = matching.first_seen_for(self.db, findings_b)
        self.assertEqual(first_seen_a, first_seen_b)
        key = "Ubuntu|openssh|1:9.6p1-3ubuntu13.3|UBUNTU-CVE-NOFIX"
        self.assertIn(key, first_seen_a)
        # A patched/absent finding drops the stored key.
        matching.first_seen_for(self.db, [])
        with open(os.path.join(self.db, "first-seen.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual({}, state)

    def test_first_seen_excludes_patched(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-FIX", "openssh", fixed="2.0")
        )
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "3.0"}]
        )
        self.assertEqual("patched", findings[0]["state"])
        self.assertEqual({}, matching.first_seen_for(self.db, findings))
        with open(os.path.join(self.db, "first-seen.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual({}, state)

    def test_multi_subrange_matches(self):
        record = {
            "id": "UBUNTU-CVE-MULTI",
            "affected": [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "openssh"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "1.0"},
                                {"fixed": "2.0"},
                                {"introduced": "3.0"},
                                {"fixed": "4.0"},
                            ],
                        }
                    ],
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        cases = {
            "1.5": "fix_available",
            "2.5": "patched",
            "3.5": "fix_available",
            "4.5": "patched",
        }
        for version, expected in cases.items():
            findings = matching.match_specs(
                self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": version}]
            )
            self.assertEqual(expected, findings[0]["state"], version)

    def test_fix_status_from_matched_subrange(self):
        record = {
            "id": "UBUNTU-CVE-SUBFIX",
            "affected": [
                {
                    "package": {"ecosystem": "Ubuntu", "name": "openssh"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "1.0"},
                                {"last_affected": "1.5"},
                                {"introduced": "2.0"},
                                {"fixed": "3.0"},
                            ],
                        }
                    ],
                }
            ],
        }
        _write_record(self.db, "Ubuntu", record)
        no_fix = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1.2"}]
        )
        self.assertEqual("no_fix_yet", no_fix[0]["state"])
        fix = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "2.5"}]
        )
        self.assertEqual("fix_available", fix[0]["state"])

    def _write_epss(self, data):
        from osv_feed import epss
        epss.save_epss(self.db, data)

    def test_epss_enriches_matching_finding(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        self._write_epss({"UBUNTU-CVE-NOFIX": {"score": 0.25, "percentile": 0.9}})
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(1, len(findings))
        self.assertEqual(0.25, findings[0]["epss_score"])
        self.assertEqual(0.9, findings[0]["epss_percentile"])

    def test_epss_looks_up_alias_when_id_missing(self):
        record = _basic_record("DEBIAN-CVE-ALIAS", "openssh")
        record["aliases"] = ["CVE-2024-9999"]
        _write_record(self.db, "Debian", record)
        self._write_epss({"CVE-2024-9999": {"score": 0.7, "percentile": 0.99}})
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Debian", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(0.7, findings[0]["epss_score"])
        self.assertEqual(0.99, findings[0]["epss_percentile"])

    def test_epss_zero_when_absent(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        self._write_epss({})
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(0.0, findings[0]["epss_score"])
        self.assertEqual(0.0, findings[0]["epss_percentile"])

    def test_epss_zero_for_uninstalled(self):
        findings = matching.match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": None}]
        )
        self.assertEqual(1, len(findings))
        self.assertEqual(matching.UNINSTALLED, findings[0]["state"])
        self.assertEqual(0.0, findings[0]["epss_score"])
        self.assertEqual(0.0, findings[0]["epss_percentile"])


if __name__ == "__main__":
    unittest.main()
