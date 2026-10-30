import json
import os
import shutil
import tempfile
import tracemalloc
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


def _match_specs(*args, **kwargs):
    return list(matching.match_specs(*args, **kwargs))


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
        findings = _match_specs(
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
            findings = _match_specs(
                self.db,
                [{"ecosystem": "Ubuntu", "package": "openssh", "version": version}],
            )
            self.assertEqual("patched", findings[0]["state"], version)

    def test_no_fix_yet(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        findings = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("no_fix_yet", findings[0]["state"])

    def test_withdrawn_record_is_ignored(self):
        record = _basic_record("UBUNTU-CVE-WITHDRAWN", "openssh")
        record["withdrawn"] = "2026-10-27T14:50:09Z"
        _write_record(self.db, "Ubuntu", record)
        findings = _match_specs(
            self.db,
            [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}],
        )
        self.assertEqual([], findings)
        self.assertEqual([], list(matching.iter_records(self.db, "Ubuntu")))

    def test_last_affected_bound(self):
        _write_record(
            self.db,
            "Ubuntu",
            _basic_record("UBUNTU-CVE-LAST", "openssh", introduced="1.0", last_affected="2.0"),
        )
        hit = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1.5"}]
        )
        miss = _match_specs(
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
        hit = _match_specs(
            self.db, [{"ecosystem": "PyPI", "package": "demo", "version": "1.0.0"}]
        )
        miss = _match_specs(
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
        findings = _match_specs(
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
        findings = _match_specs(
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
        findings = _match_specs(
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
        findings = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual("critical", findings[0]["severity"])

    def test_severity_empty_when_unavailable(self):
        _write_record(self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOSEV", "openssh"))
        findings = _match_specs(
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
        findings = _match_specs(
            self.db,
            [{"ecosystem": "Go", "package": "github.com/fatedier/frp", "version": "0.70.0"}],
        )
        self.assertEqual("high", findings[0]["severity"])

    def test_severity_cvss4_vector_without_label_is_conservative(self):
        # Alpine OSV records publish a CVSS 4.0 vector but no qualitative
        # label; a network-reachable high-impact vector must still classify as
        # critical so the container policy does not drop it.
        record = {
            "id": "ALPINE-CVE-CVSS4",
            "severity": [
                {
                    "type": "CVSS_V4",
                    "score": "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
                }
            ],
            "affected": [{"package": {"name": "brotli"}}],
        }
        _write_record(self.db, "Alpine", record)
        findings = _match_specs(
            self.db,
            [{"ecosystem": "Alpine", "package": "brotli", "version": "1.2.0-r1"}],
        )
        self.assertEqual("critical", findings[0]["severity"])

    def test_severity_cvss4_availability_only_is_high(self):
        record = {
            "id": "PYSEC-CVSS4-DOS",
            "severity": [
                {
                    "type": "CVSS_V4",
                    "score": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:N",
                }
            ],
            "affected": [{"package": {"name": "pypdf"}}],
        }
        _write_record(self.db, "PyPI", record)
        findings = _match_specs(
            self.db,
            [{"ecosystem": "PyPI", "package": "pypdf", "version": "6.7.5"}],
        )
        self.assertEqual("high", findings[0]["severity"])

    def test_severity_cvss4_confidentiality_only_is_high(self):
        record = {
            "id": "ALPINE-CVE-CVSS4-READ",
            "severity": [
                {
                    "type": "CVSS_V4",
                    "score": "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:H/VI:N/VA:L/SC:N/SI:N/SA:N",
                }
            ],
            "affected": [{"package": {"name": "expat"}}],
        }
        _write_record(self.db, "Alpine", record)
        findings = _match_specs(
            self.db,
            [{"ecosystem": "Alpine", "package": "expat", "version": "2.8.5-r0"}],
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
            findings = _match_specs(
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
        records = list(matching.iter_candidate_records(self.db, "Ubuntu", ["openssh"]))
        self.assertEqual(["UBUNTU-CVE-A"], [record["id"] for record in records])

    def test_candidate_records_are_parsed_lazily(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-A", "openssh")
        )
        with mirror.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO records(ecosystem, id, data) VALUES (?, ?, ?)",
                ("Ubuntu", "UBUNTU-CVE-BAD", "{not-json"),
            )
            conn.execute(
                "INSERT INTO packages(ecosystem, package, record_id) "
                "VALUES (?, ?, ?)",
                ("Ubuntu", "openssh", "UBUNTU-CVE-BAD"),
            )

        records = matching.iter_candidate_records(
            self.db, "Ubuntu", ["openssh"]
        )
        self.assertEqual("UBUNTU-CVE-A", next(records)["id"])
        with self.assertRaises(ValueError):
            next(records)

    def test_match_specs_is_lazy(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-A", "openssh")
        )
        with mirror.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO records(ecosystem, id, data) VALUES (?, ?, ?)",
                ("Ubuntu", "UBUNTU-CVE-BAD", "{not-json"),
            )
            conn.execute(
                "INSERT INTO packages(ecosystem, package, record_id) "
                "VALUES (?, ?, ?)",
                ("Ubuntu", "openssh", "UBUNTU-CVE-BAD"),
            )

        findings = matching.match_specs(
            self.db,
            [{
                "ecosystem": "Ubuntu",
                "package": "openssh",
                "version": "1:9.6p1-3ubuntu13.3",
            }],
        )
        self.assertEqual("UBUNTU-CVE-A", next(findings)["cve"])
        with self.assertRaises(ValueError):
            next(findings)

    def test_match_specs_keeps_only_one_record(self):
        versions = [f"9.{i}.0" for i in range(20000)]
        versions[0] = "1"
        for i in range(20):
            _write_record(
                self.db,
                "Ubuntu",
                {
                    "id": f"UBUNTU-CVE-LARGE-{i}",
                    "summary": "large record",
                    "affected": [
                        {
                            "package": {
                                "ecosystem": "Ubuntu",
                                "name": "openssh",
                            },
                            "versions": versions,
                        }
                    ],
                },
            )

        tracemalloc.start()
        try:
            count = sum(
                1
                for _ in matching.match_specs(
                    self.db,
                    [{
                        "ecosystem": "Ubuntu",
                        "package": "openssh",
                        "version": "1",
                    }],
                )
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(20, count)
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_candidate_records_immutable_read(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-A", "openssh")
        )
        records = list(
            matching.iter_candidate_records(
                self.db, "Ubuntu", ["openssh"], immutable=True
            )
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
        findings_a = _match_specs(self.db, specs)
        findings_b = _match_specs(self.db, specs)
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
        findings = _match_specs(
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
            findings = _match_specs(
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
        no_fix = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1.2"}]
        )
        self.assertEqual("no_fix_yet", no_fix[0]["state"])
        fix = _match_specs(
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
        findings = _match_specs(
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
        findings = _match_specs(
            self.db, [{"ecosystem": "Debian", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(0.7, findings[0]["epss_score"])
        self.assertEqual(0.99, findings[0]["epss_percentile"])

    def test_epss_zero_when_absent(self):
        _write_record(
            self.db, "Ubuntu", _basic_record("UBUNTU-CVE-NOFIX", "openssh")
        )
        self._write_epss({})
        findings = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": "1:9.6p1-3ubuntu13.3"}]
        )
        self.assertEqual(0.0, findings[0]["epss_score"])
        self.assertEqual(0.0, findings[0]["epss_percentile"])

    def test_epss_zero_for_uninstalled(self):
        findings = _match_specs(
            self.db, [{"ecosystem": "Ubuntu", "package": "openssh", "version": None}]
        )
        self.assertEqual(1, len(findings))
        self.assertEqual(matching.UNINSTALLED, findings[0]["state"])
        self.assertEqual(0.0, findings[0]["epss_score"])
        self.assertEqual(0.0, findings[0]["epss_percentile"])


class EcosystemVersionCompareTest(unittest.TestCase):
    def test_go_versions_strip_v_prefix(self):
        self.assertLess(matching.go_version_compare("v0.1.0", "0.52.0"), 0)
        self.assertEqual(matching.go_version_compare("v1.2.3", "v1.2.3"), 0)

    def test_go_prerelease_sorts_before_release(self):
        self.assertLess(
            matching.go_version_compare("v1.2.3-rc1", "v1.2.3"), 0
        )
        self.assertGreater(
            matching.go_version_compare("v1.2.3", "v1.2.3-rc1"), 0
        )

    def test_go_pseudo_version_compares(self):
        self.assertEqual(
            matching.go_version_compare(
                "v0.0.0-20201216223049-8b5274cf687f",
                "0.0.0-20201216223049-8b5274cf687f",
            ),
            0,
        )

    def test_alpine_revision_and_suffix_order(self):
        self.assertLess(
            matching.alpine_version_compare("1.2.3-r1", "1.2.3-r2"), 0
        )
        self.assertGreater(
            matching.alpine_version_compare("1.2.3-r10", "1.2.3-r2"), 0
        )
        self.assertLess(
            matching.alpine_version_compare("1.2.3_alpha1", "1.2.3"), 0
        )
        self.assertGreater(
            matching.alpine_version_compare("1.2.3_p1", "1.2.3"), 0
        )

    def test_match_specs_uses_go_comparator(self):
        db = tempfile.mkdtemp(prefix="osv-feed-test-")
        self.addCleanup(shutil.rmtree, db, ignore_errors=True)
        _write_record(
            db,
            "Go",
            _basic_record("GO-CVE-VPREFIX", "example.com/mod", fixed="0.52.0"),
        )
        findings = _match_specs(
            db,
            [{"ecosystem": "Go", "package": "example.com/mod", "version": "v0.1.0"}],
        )
        self.assertEqual(1, len(findings))
        self.assertEqual("fix_available", findings[0]["state"])

    def test_match_specs_uses_alpine_comparator(self):
        db = tempfile.mkdtemp(prefix="osv-feed-test-")
        self.addCleanup(shutil.rmtree, db, ignore_errors=True)
        record = _basic_record("ALPINE-CVE-REV", "openssl", fixed="1.2.3-r2")
        record["affected"][0]["package"]["ecosystem"] = "Alpine:v3.24"
        _write_record(db, "Alpine", record)
        finding = _match_specs(
            db,
            [{"ecosystem": "Alpine:v3.24", "package": "openssl", "version": "1.2.3_alpha1"}],
        )
        self.assertEqual("fix_available", finding[0]["state"])
        patched = _match_specs(
            db,
            [{"ecosystem": "Alpine:v3.24", "package": "openssl", "version": "1.2.3-r2"}],
        )
        self.assertEqual("patched", patched[0]["state"])

    def test_alpine_release_filters_other_release_entries(self):
        db = tempfile.mkdtemp(prefix="osv-feed-test-")
        self.addCleanup(shutil.rmtree, db, ignore_errors=True)
        record = _basic_record("ALPINE-CVE-RELEASES", "gnutls", fixed="3.5.13-r0")
        record["affected"].append(
            {
                "package": {"ecosystem": "Alpine:v3.3", "name": "gnutls"},
                "ranges": [
                    {"type": "ECOSYSTEM",
                     "events": [{"introduced": "0"}, {"fixed": "3.14.17-r1"}]}
                ],
            }
        )
        record["affected"][0]["package"]["ecosystem"] = "Alpine:v3.24"
        _write_record(db, "Alpine", record)
        findings = _match_specs(
            db,
            [{"ecosystem": "Alpine:v3.24", "package": "gnutls", "version": "3.8.13-r0"}],
        )
        self.assertEqual("patched", findings[0]["state"])


if __name__ == "__main__":
    unittest.main()
