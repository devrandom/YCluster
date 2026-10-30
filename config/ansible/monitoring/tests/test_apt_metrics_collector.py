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


if __name__ == "__main__":
    unittest.main()
