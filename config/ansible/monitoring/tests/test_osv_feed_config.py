"""Rendered osv-feed config guards.

The kernel category is matched from the compact index, not the mirror. If
`source = "index"` goes missing from the rendered table, the 15-minute
collector silently starts scanning several GB of kernel advisories on every
run — the exact failure this test exists to prevent.
"""

import pathlib
import unittest

import jinja2
import json

TEMPLATE = pathlib.Path(__file__).parent.parent / "templates" / "osv-feed-config.toml.j2"
ALERTS = pathlib.Path(__file__).parent.parent / "templates" / "ycluster-alerts.yml.j2"


class KernelCategoryTest(unittest.TestCase):
    def render(self) -> str:
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATE.parent)),
            undefined=jinja2.StrictUndefined,
        )
        # Ansible provides `to_json`; the template only needs the filter name.
        environment.filters["to_json"] = lambda value: json.dumps(list(value))
        return environment.get_template(TEMPLATE.name).render(
            osv_feed_db_dir="/var/lib/osv-feed/db",
            osv_feed_ack_db="/near1/osv-feed/acks.json",
            osv_feed_ecosystem="Ubuntu",
            osv_feed_perimeter_packages=["openssh"],
            osv_feed_perimeter_severity="high",
            osv_feed_network_packages=["wireguard"],
            osv_feed_runtimes_packages=["podman"],
            osv_feed_selfbuilt_ecosystem="Go",
            osv_feed_selfbuilt_packages=["github.com/fatedier/frp=0.70.1"],
            osv_feed_selfbuilt_severity="high",
            osv_feed_container_severity="high",
            osv_feed_kernel_versions=["linux=6.8.0-149.149"],
            osv_feed_kernel_severity="medium",
        )

    def test_kernel_category_is_index_sourced(self):
        rendered = self.render()
        block = rendered.split("[tracking.kernel]", 1)[1].split("[tracking.", 1)[0]
        self.assertIn('source = "index"', block)
        self.assertIn('"linux=6.8.0-149.149"', block)


class KernelAlertTest(unittest.TestCase):
    def test_kernel_alert_is_coalesced_and_repeats_daily(self):
        """One fleet-wide alert, not one per running kernel version.

        A per-version expression makes the ntfy webhook push once for every
        kernel line on every repeat. The collector's summary sample and the
        slow_repeat route keep it to one daily reminder instead.
        """
        text = ALERTS.read_text()
        block = text.split("- alert: KernelCVEPatchAvailable", 1)[1].split("- alert:", 1)[0]
        self.assertIn('osv_feed_kernel_summary{state="fix_available"}', block)
        self.assertNotIn("sum by (package, version)", block)
        self.assertIn('slow_repeat: "true"', block)


if __name__ == "__main__":
    unittest.main()
