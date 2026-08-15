import json
import os
import shutil
import tempfile
import unittest

from osv_feed import acks


class AcksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-acks-")
        self.path = os.path.join(self.tmp, "acks.json")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_add_list_persists(self):
        acks.add_ack(
            self.path,
            category="perimeter",
            ecosystem="Debian",
            cve="DEBIAN-CVE-2026-60000",
            note="blocked by firewall",
        )
        entries = acks.list_acks(self.path)
        self.assertEqual(1, len(entries))
        self.assertEqual("perimeter", entries[0]["category"])
        self.assertEqual("blocked by firewall", entries[0]["note"])
        self.assertIn("acked_at", entries[0])

        # A new process (same file) sees the acknowledgment.
        reloaded = acks.list_acks(self.path)
        self.assertEqual(entries, reloaded)

    def test_add_replaces_existing_entry(self):
        acks.add_ack(self.path, category="user", ecosystem="Ubuntu", cve="UBUNTU-CVE-1")
        first = acks.list_acks(self.path)[0]["acked_at"]
        acks.add_ack(self.path, category="user", ecosystem="Ubuntu", cve="UBUNTU-CVE-1", note="updated")
        entries = acks.list_acks(self.path)
        self.assertEqual(1, len(entries))
        self.assertEqual("updated", entries[0]["note"])
        self.assertGreaterEqual(entries[0]["acked_at"], first)

    def test_remove(self):
        acks.add_ack(self.path, category="perimeter", ecosystem="Debian", cve="DEBIAN-CVE-X")
        self.assertTrue(
            acks.remove_ack(self.path, category="perimeter", ecosystem="Debian", cve="DEBIAN-CVE-X")
        )
        self.assertEqual([], acks.list_acks(self.path))
        self.assertFalse(
            acks.remove_ack(self.path, category="perimeter", ecosystem="Debian", cve="DEBIAN-CVE-X")
        )

    def test_list_filters_by_category(self):
        acks.add_ack(self.path, category="perimeter", ecosystem="Debian", cve="D1")
        acks.add_ack(self.path, category="user", ecosystem="Ubuntu", cve="U1")
        self.assertEqual(["D1"], [entry["cve"] for entry in acks.list_acks(self.path, "perimeter")])

    def test_key_is_category_ecosystem_cve(self):
        self.assertEqual("perimeter|Debian|CVE-1", acks.ack_key("perimeter", "Debian", "CVE-1"))

    def test_invalid_db_raises(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[bad json")
        with self.assertRaises(ValueError):
            acks.list_acks(self.path)

    def test_required_fields(self):
        with self.assertRaises(ValueError):
            acks.add_ack(self.path, category="", ecosystem="Debian", cve="CVE-1")
        with self.assertRaises(ValueError):
            acks.add_ack(self.path, category="perimeter", ecosystem="Debian", cve="")

    def test_wildcard_ecosystem(self):
        entry = acks.add_ack(
            self.path, category="perimeter", ecosystem="", cve="DEBIAN-CVE-1"
        )
        self.assertEqual("", entry["ecosystem"])
        self.assertEqual(
            "perimeter||DEBIAN-CVE-1",
            acks.ack_key(entry["category"], entry["ecosystem"], entry["cve"]),
        )
        entries = acks.list_acks(self.path)
        self.assertEqual("", entries[0]["ecosystem"])
        self.assertEqual(["DEBIAN-CVE-1"], [e["cve"] for e in entries])


if __name__ == "__main__":
    unittest.main()
