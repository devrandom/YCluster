"""Validate config/ansible/perimeter_software.yml, the single source of truth
for the osv-feed tracking lists consumed by monitoring/install-osv-feed.yml.

Deployment-specific (lives outside the general osv-feed tool): it checks that
the documentation inventory (tier, nodes, owners) and the collector config
(which packages each [tracking.*] table matches) do not drift.
"""

import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - only here for dep-free test hosts
    yaml = None

INVENTORY = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "../../perimeter_software.yml"))


@unittest.skipIf(yaml is None, "pyyaml not available")
class TestPerimeterInventory(unittest.TestCase):

    # Only machine-consumed fields belong in the inventory; package/bin name,
    # pin source, and manifest path are comments, not fields.
    REQUIRED = ("name", "tier", "osv_category", "osv_packages")

    # osv-packages each tracking table must derive from the inventory.
    # Mirrors the package strings the collector config matched before the
    # inventory became the source of truth.
    EXPECTED = {
        "perimeter": ["certbot", "iptables", "nginx", "openssh", "openssl",
                      "python3-certbot-nginx", "ufw"],
        "network": ["dnsmasq", "keepalived", "prometheus-blackbox-exporter",
                    "squid", "wireguard", "wireguard-tools"],
        "runtimes": ["containerd", "docker.io", "podman", "runc"],
        # selfbuilt entries dedupe onto a single frp pin (frps + frpc).
        "selfbuilt": ["github.com/fatedier/frp=0.70.1"],
    }

    def setUp(self):
        with open(INVENTORY) as fh:
            self.entries = yaml.safe_load(fh)["perimeter_software"]

    def test_inventory_is_populated(self):
        self.assertGreaterEqual(len(self.entries), 16)

    def test_every_entry_has_required_fields(self):
        missing = []
        for e in self.entries:
            for key in self.REQUIRED:
                if key not in e:
                    missing.append((e.get("name"), key))
        self.assertEqual(missing, [])

    def test_osv_packages_are_lists_or_absent(self):
        for e in self.entries:
            if "osv_packages" in e:
                self.assertIsInstance(e["osv_packages"], list,
                                      f"{e['name']}: osv_packages not a list")
                self.assertTrue(all(isinstance(p, str) for p in e["osv_packages"]),
                                f"{e['name']}: osv_packages not all strings")

    def test_derived_tracking_sets_match_expected(self):
        for cat, expected in self.EXPECTED.items():
            derived = []
            for e in self.entries:
                if e.get("osv_category") == cat:
                    derived += e.get("osv_packages", [])
            self.assertEqual(sorted(set(derived)), sorted(expected),
                             f"tracking.{cat} package set drifted from the "
                             "perimeter inventory")

    def test_container_entries_are_manifest_driven(self):
        for e in self.entries:
            if e.get("osv_category") != "container":
                continue
            self.assertEqual(e.get("osv_packages", []), [],
                             f"{e['name']}: container entries are "
                             "manifest-driven, no osv_packages")
            # Matching is by the manifest file in the collector's container
            # dir; the manifest path itself is only a human comment. Untracked
            # images (no manifest yet) are flagged via `untracked` and
            # contribute no packages either way.

    def test_comfyui_is_documented_but_untracked(self):
        comfy = [e for e in self.entries if e["name"] == "comfyui"]
        self.assertEqual(len(comfy), 1)
        self.assertEqual(comfy[0]["osv_category"], "container")
        self.assertEqual(comfy[0]["tier"], "P0-A")
        # No osv-feed container manifest generated for the GPU image yet, so it
        # must be marked untracked and contribute no packages.
        self.assertIs(comfy[0].get("untracked"), True)
        self.assertEqual(comfy[0].get("osv_packages", []), [])

    def test_incus_is_documented_but_untracked(self):
        incus = [e for e in self.entries if "Incus" in e["name"]]
        self.assertEqual(len(incus), 1)
        self.assertEqual(incus[0]["osv_category"], "runtimes")
        # Not yet covered by the osv-feed runtimes table, so it must
        # contribute no packages to the derived list.
        self.assertEqual(incus[0].get("osv_packages", []), [])


if __name__ == "__main__":
    unittest.main()
