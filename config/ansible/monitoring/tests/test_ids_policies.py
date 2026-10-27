"""Static checks for the pinned Tetragon IDS policy set."""

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

ROOT = Path(__file__).resolve().parents[4]
POLICY_DIR = ROOT / "config/ansible/app/files/tetragon-policies"
TETRAGON_CONFIG = ROOT / "config/ansible/monitoring/templates/tetragon.yaml.j2"
TETRAGON_INSTALL = ROOT / "config/ansible/monitoring/install-tetragon.yml"


@unittest.skipIf(yaml is None, "PyYAML is not available")
class TestIDSPolicies(unittest.TestCase):
    def load_policy(self, name):
        with (POLICY_DIR / name).open() as fh:
            return yaml.safe_load(fh)

    def all_probes(self):
        probes = []
        for path in sorted(POLICY_DIR.glob("*.yaml")):
            with path.open() as fh:
                doc = yaml.safe_load(fh)
            probes.extend(doc["spec"]["kprobes"])
        return probes

    def test_generic_credential_hooks_are_absent(self):
        calls = {probe["call"] for probe in self.all_probes()}
        self.assertNotIn("commit_creds", calls)
        self.assertNotIn("security_kernel_module_request", calls)

    def test_capability_gains_compare_old_and_new_sets(self):
        policy = self.load_policy("privilege-raises.yaml")
        capset = next(
            probe for probe in policy["spec"]["kprobes"]
            if probe["call"] == "security_capset"
        )
        self.assertEqual(len(capset["args"]), 4)
        selectors = capset["selectors"]
        gains = [
            selector["matchArgs"][0]["args"]
            for selector in selectors
            if selector["matchArgs"][0]["operator"] == "CapabilitiesGained"
        ]
        self.assertEqual(gains, [[0, 1], [2, 3]])
        for selector in selectors:
            action = selector["matchActions"][0]
            self.assertEqual(action["rateLimitScope"], "process")

    def test_setuid_root_is_targeted_and_rate_limited(self):
        policy = self.load_policy("privilege-raises.yaml")
        setuid = next(
            probe for probe in policy["spec"]["kprobes"]
            if probe["call"] == "__sys_setuid"
        )
        selector = setuid["selectors"][0]
        self.assertEqual(selector["matchArgs"][0]["values"], ["0"])
        self.assertEqual(selector["matchActions"][0]["rateLimit"], "1m")
        self.assertEqual(selector["matchActions"][0]["rateLimitScope"], "process")

    def test_kernel_modules_match_actual_loads(self):
        policy = self.load_policy("kernel-modules.yaml")
        calls = {probe["call"] for probe in policy["spec"]["kprobes"]}
        self.assertEqual(calls, {"security_kernel_read_file", "do_init_module"})
        read_probe = next(
            probe for probe in policy["spec"]["kprobes"]
            if probe["call"] == "security_kernel_read_file"
        )
        suffixes = read_probe["selectors"][0]["matchArgs"][0]["values"]
        self.assertIn(".ko", suffixes)
        self.assertIn(".ko.zst", suffixes)

    def test_tcp_policy_uses_tcp_connect(self):
        policy = self.load_policy("tcp-connect.yaml")
        probe = policy["spec"]["kprobes"][0]
        self.assertEqual(probe["call"], "tcp_connect")
        self.assertEqual(probe["args"], [{"index": 0, "type": "sock"}])
        selector = probe["selectors"][0]["matchArgs"][0]
        self.assertEqual(selector["operator"], "NotDAddr")
        self.assertIn("127.0.0.0/8", selector["values"])
        self.assertIn("::1/128", selector["values"])
        action = probe["selectors"][0]["matchActions"][0]
        self.assertEqual(action["rateLimit"], "1m")
        self.assertEqual(action["rateLimitScope"], "process")

    def test_process_exit_is_not_exported(self):
        text = TETRAGON_CONFIG.read_text()
        self.assertIn('"PROCESS_EXEC"', text)
        self.assertIn('"PROCESS_KPROBE"', text)
        self.assertNotIn('"PROCESS_EXIT"', text)

    def test_install_removes_retired_policy(self):
        text = TETRAGON_INSTALL.read_text()
        self.assertIn("Remove retired tracing policies", text)
        self.assertIn("privilege-and-capability-changes.yaml", text)


if __name__ == "__main__":
    unittest.main()
