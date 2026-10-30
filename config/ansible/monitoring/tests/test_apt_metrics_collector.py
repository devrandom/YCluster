"""Unit tests for the apt/needrestart metrics collector."""

import importlib.util
import unittest
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parents[1] / "files" / "apt-metrics-collector.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("apt_metrics_collector", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestParseNeedrestart(unittest.TestCase):
    def setUp(self):
        self.collector = load_collector()

    def test_kernel_upgrade_and_services(self):
        services, kernel = self.collector.parse_needrestart(
            "NEEDRESTART-VER: 3.6\n"
            "NEEDRESTART-KCUR: 6.8.0-139-generic\n"
            "NEEDRESTART-KEXP: 6.8.0-147-generic\n"
            "NEEDRESTART-KSTA: 3\n"
            "NEEDRESTART-SVC: nginx.service\n"
            "NEEDRESTART-SVC: squid.service\n"
        )
        self.assertEqual(services, ["nginx.service", "squid.service"])
        self.assertEqual(kernel, 1)

    def test_greylisted_services_and_current_kernel(self):
        services, kernel = self.collector.parse_needrestart(
            "NEEDRESTART-KSTA: 1\n"
            "NEEDRESTART-SVC: dbus.service\n"
            "NEEDRESTART-SVC: systemd-logind.service\n"
            "NEEDRESTART-SVC: unattended-upgrades.service\n"
            "NEEDRESTART-SVC: getty@tty1.service\n"
            "NEEDRESTART-SVC: user@1000.service\n"
        )
        self.assertEqual(services, [])
        self.assertEqual(kernel, 0)

    def test_unknown_kernel_status_is_not_pending(self):
        services, kernel = self.collector.parse_needrestart("NEEDRESTART-KSTA: 0\n")
        self.assertEqual(services, [])
        self.assertEqual(kernel, 0)


class TestParseConffilePrompts(unittest.TestCase):
    def setUp(self):
        self.collector = load_collector()

    def test_last_run_wins(self):
        text = (
            "2026-10-01 06:00:00,000 INFO Starting unattended upgrades script\n"
            "2026-10-01 06:00:01,000 WARNING Package squid has conffile prompt "
            "and needs to be upgraded manually\n"
            "2026-10-02 06:00:00,000 INFO Starting unattended upgrades script\n"
            "2026-10-02 06:00:02,000 INFO No packages found that can be "
            "upgraded unattended and no pending auto-removals\n"
        )
        self.assertEqual(self.collector.parse_conffile_prompts(text), [])

    def test_packages_deduplicated_and_sorted(self):
        text = (
            "2026-10-02 06:00:00,000 INFO Starting unattended upgrades script\n"
            "2026-10-02 06:00:01,000 WARNING Package squid has conffile prompt "
            "and needs to be upgraded manually\n"
            "2026-10-02 06:00:02,000 WARNING Package nginx-common has conffile "
            "prompt and needs to be upgraded manually\n"
            "2026-10-02 06:00:03,000 WARNING Package squid has conffile prompt "
            "and needs to be upgraded manually\n"
        )
        self.assertEqual(
            self.collector.parse_conffile_prompts(text),
            ["nginx-common", "squid"],
        )

    def test_blacklist_lines_are_not_prompts(self):
        text = (
            "2026-10-02 06:00:00,000 INFO Starting unattended upgrades script\n"
            "2026-10-02 06:00:02,000 INFO Package squid is blacklisted.\n"
        )
        self.assertEqual(self.collector.parse_conffile_prompts(text), [])

    def test_empty_log(self):
        self.assertEqual(self.collector.parse_conffile_prompts(""), [])


class TestParseAptUpdateFailures(unittest.TestCase):
    def setUp(self):
        self.collector = load_collector()

    def test_unreachable_mirror_reported_once(self):
        sources = self.collector.parse_apt_update_failures(
            "Hit:1 http://security.ubuntu.com/ubuntu noble-security InRelease\n"
            "Err:3 http://pt.archive.ubuntu.com/ubuntu noble InRelease\n"
            "W: Failed to fetch http://pt.archive.ubuntu.com/ubuntu/dists/noble/InRelease"
            "  Could not connect to pt.archive.ubuntu.com:80, connection timed out\n"
            "W: Some index files failed to download.\n"
        )
        self.assertEqual(sources, ["http://pt.archive.ubuntu.com/ubuntu"])

    def test_clean_run_has_no_failures(self):
        sources = self.collector.parse_apt_update_failures(
            "Hit:1 http://fr.archive.ubuntu.com/ubuntu noble InRelease\n"
            "Get:2 http://fr.archive.ubuntu.com/ubuntu noble-updates InRelease [126 kB]\n"
        )
        self.assertEqual(sources, [])


if __name__ == "__main__":
    unittest.main()
