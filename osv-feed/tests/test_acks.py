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

    def test_key_with_surface_is_four_part(self):
        self.assertEqual(
            "container|PyPI|GHSA-1|open-webui",
            acks.ack_key("container", "PyPI", "GHSA-1", "open-webui"),
        )

    def test_add_ack_stores_surface(self):
        entry = acks.add_ack(
            self.path,
            category="container",
            ecosystem="PyPI",
            cve="GHSA-1",
            surface="open-webui",
            note="reviewed unreachable",
        )
        self.assertEqual("open-webui", entry["surface"])
        self.assertEqual(
            "container|PyPI|GHSA-1|open-webui",
            acks.ack_key(entry["category"], entry["ecosystem"], entry["cve"], entry["surface"]),
        )

    def test_remove_ack_with_surface(self):
        acks.add_ack(
            self.path,
            category="container",
            ecosystem="PyPI",
            cve="GHSA-1",
            surface="open-webui",
        )
        self.assertTrue(
            acks.remove_ack(
                self.path,
                category="container",
                ecosystem="PyPI",
                cve="GHSA-1",
                surface="open-webui",
            )
        )
        self.assertEqual([], acks.list_acks(self.path))
        # A surface-less remove must NOT match a surface-scoped ack.
        self.assertFalse(
            acks.remove_ack(self.path, category="container", ecosystem="PyPI", cve="GHSA-1")
        )

    def test_list_filters_by_surface(self):
        acks.add_ack(
            self.path, category="container", ecosystem="PyPI", cve="G1", surface="open-webui"
        )
        acks.add_ack(
            self.path, category="container", ecosystem="PyPI", cve="G2", surface="zulip"
        )
        acks.add_ack(self.path, category="container", ecosystem="PyPI", cve="G3")
        entries = acks.list_acks(self.path, surface="zulip")
        self.assertEqual(["G2"], [e["cve"] for e in entries])
        # No surface filter returns everything, wildcard ack included.
        self.assertEqual(3, len(acks.list_acks(self.path)))

    def test_find_ack_prefers_exact_ecosystem_and_surface(self):
        store = {}
        # Exact ecosystem + exact surface wins over a wildcard surface ack.
        store[acks.ack_key("container", "PyPI", "G1", "open-webui")] = {
            "surface": "open-webui"
        }
        store[acks.ack_key("container", "PyPI", "G1")] = {"surface": ""}
        got = acks.find_ack(store, "container", "PyPI", "G1", "open-webui")
        self.assertEqual("open-webui", got["surface"])

    def test_find_ack_surface_scoped_does_not_leak_to_other_surfaces(self):
        store = {
            acks.ack_key("container", "PyPI", "G1", "open-webui"): {"surface": "open-webui"}
        }
        # Different surface / no surface must NOT match the open-webui ack.
        self.assertFalse(acks.find_ack(store, "container", "PyPI", "G1", "zulip"))
        self.assertFalse(acks.find_ack(store, "container", "PyPI", "G1"))

    def test_find_ack_wildcard_surface_matches_any_surface(self):
        store = {acks.ack_key("container", "PyPI", "G1"): {"surface": ""}}
        self.assertIsNotNone(acks.find_ack(store, "container", "PyPI", "G1", "open-webui"))
        self.assertIsNotNone(acks.find_ack(store, "container", "PyPI", "G1"))

    def test_find_ack_wildcard_ecosystem(self):
        store = {acks.ack_key("container", "", "G1", "open-webui"): {"surface": "open-webui"}}
        self.assertIsNotNone(acks.find_ack(store, "container", "PyPI", "G1", "open-webui"))
        self.assertFalse(acks.find_ack(store, "container", "PyPI", "G1", "zulip"))

    def test_find_ack_wildcard_everything(self):
        store = {acks.ack_key("container", "", "G1"): {"surface": ""}}
        self.assertIsNotNone(acks.find_ack(store, "container", "PyPI", "G1", "open-webui"))
        self.assertFalse(acks.find_ack(store, "container", "PyPI", "G2", "open-webui"))


if __name__ == "__main__":
    unittest.main()
