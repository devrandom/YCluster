"""Unit tests for the bootstrap vendor-account guards.

The nvidia/nas/macos and the remote wg/wg-macos bootstrap templates each
carry a guard that fails bring-up if a non-baseline privileged account is
present on a fresh vendor image (see todo/archive/vendor-account-cleanup.md).
These tests render the real templates through the same Jinja engine the admin
app serves, extract the guard block, and execute it in /bin/bash against
stubbed getent (Linux) / dscl (macOS) so the pass/fail semantics are pinned
without needing real nodes of each type.

Run from config/ansible/admin/files:
    PYTHONPATH=.:ycluster python3 -m unittest discover -s tests
(or via ./run-tests.sh at the repo root).
"""

import os
import shlex
import subprocess
import tempfile
import unittest

import jinja2

_HERE = os.path.dirname(os.path.abspath(__file__))       # .../admin/files/tests
_FILES = os.path.dirname(_HERE)                           # .../admin/files
_TEMPLATES = os.path.join(_FILES, "templates")

LINUX_START = 'echo "Checking for unexpected privileged (sudo/lxd) accounts..."'
MACOS_START = 'echo "Checking for unexpected admin-group accounts..."'
GUARD_END = 'echo "Privileged account check passed"'

LINUX_TEMPLATES = ("nvidia-bootstrap.sh.j2",
                   "nas-bootstrap.sh.j2",
                   "wg-bootstrap.sh.j2")
MACOS_TEMPLATES = ("macos-bootstrap.sh.j2",
                   "wg-macos-bootstrap.sh.j2")


def _render(name):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES))
    template = env.get_template(name)
    return template.render(api_server="http://10.0.0.53",
                           ssh_key_content="ssh-ed25519 AAAA test")


def _extract_guard(rendered, start):
    lines = rendered.splitlines()
    si = next(i for i, l in enumerate(lines) if l.strip() == start)
    ei = next(i for i, l in enumerate(lines) if l.strip() == GUARD_END)
    return "\n".join(lines[si:ei + 1]) + "\n"


def _case_items(items, indent):
    """Format `name) body;;` case items with the given indent."""
    return "\n".join("      %s) %s;;" % (name, body) for name, body in items)


def _getent_stub(groups, passwd):
    """Shell function mimicking `getent group|passwd <name>`.

    groups: dict name -> list of members. passwd: dict name -> uid.
    A group/passwd entry absent from the dict is treated as not found
    (non-zero exit), like getent against a system without the entry.
    """
    group_items = [(n, 'echo "%s:x:27:%s"' % (n, ",".join(m)))
                   for n, m in sorted(groups.items())]
    pw_items = [(n, 'echo "%s:x:%d:%d::/home/%s:/bin/bash"'
                    % (n, uid, uid, n))
                for n, uid in sorted(passwd.items())]
    return (
        "getent() {\n"
        '  local what="$1" n="$2"\n'
        '  case "$what" in\n'
        "    group)\n"
        '      case "$n" in\n'
        "%s\n"
        "      *) return 2;;\n"
        "      esac\n"
        "      ;;\n"
        "    passwd)\n"
        '      case "$n" in\n'
        "%s\n"
        "      *) return 2;;\n"
        "      esac\n"
        "      ;;\n"
        "  esac\n"
        "}\n" % (_case_items(group_items, 6), _case_items(pw_items, 6)))


def _dscl_stub(membership):
    """Shell function mimicking `dscl . -read /Groups/admin GroupMembership`."""
    return ("dscl() {\n"
            "  echo %s\n"
            "}\n" % shlex.quote(membership))


def _run_guard(stub_preamble, guard):
    script = "set -euo pipefail\n" + stub_preamble + "\n" + guard
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        p = subprocess.run(["bash", path], capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr)
    finally:
        os.unlink(path)


class LinuxVendorGuardTest(unittest.TestCase):
    def _check(self, groups, passwd, expect_fail, label):
        for name in LINUX_TEMPLATES:
            guard = _extract_guard(_render(name), LINUX_START)
            stub = _getent_stub(groups, passwd)
            rc, out = _run_guard(stub, guard)
            if expect_fail:
                self.assertNotEqual(rc, 0, "%s: %s should fail" % (name, label))
                self.assertIn("vendor preinstall account", out)
            else:
                self.assertEqual(rc, 0, "%s: %s should pass (%s)"
                                 % (name, label, out))

    def test_guard_is_present(self):
        for name in LINUX_TEMPLATES:
            self.assertIn(LINUX_START, _render(name))

    def test_clean_admin_root_only_passes(self):
        self._check({"sudo": ["admin"], "lxd": ["root"]},
                    {"admin": 1001, "root": 0}, False, "no vendor account")

    def test_groups_absent_passes(self):
        self._check({}, {"admin": 1001}, False, "groups absent")

    def test_vendor_in_sudo_fails(self):
        self._check({"sudo": ["admin", "exx"], "lxd": ["root"]},
                    {"admin": 1001, "exx": 1000, "root": 0},
                    True, "vendor account in sudo")

    def test_vendor_in_lxd_fails(self):
        self._check({"sudo": ["admin"], "lxd": ["foresight"]},
                    {"admin": 1001, "foresight": 1000},
                    True, "vendor account in lxd")

    def test_dev_in_sudo_fails(self):
        # dev is *not* a baseline sudo/lxd member on Linux (no playbook adds
        # it); its presence there is drifted state the guard should catch.
        self._check({"sudo": ["admin", "dev"], "lxd": ["root"]},
                    {"admin": 1001, "dev": 1000, "root": 0},
                    True, "dev in sudo is drift on linux")


class MacosVendorGuardTest(unittest.TestCase):
    def _check(self, membership, expect_fail, label):
        for name in MACOS_TEMPLATES:
            guard = _extract_guard(_render(name), MACOS_START)
            # The wg-macos template invokes dscl/awk by absolute path, which a
            # shell-function stub cannot shadow. Normalize to the bare names for
            # the test so both templates exercise identical logic.
            guard = guard.replace("/usr/bin/dscl", "dscl")
            guard = guard.replace("/usr/bin/awk", "awk")
            stub = _dscl_stub(membership)
            rc, out = _run_guard(stub, guard)
            if expect_fail:
                self.assertNotEqual(rc, 0, "%s: %s should fail" % (name, label))
                self.assertIn("vendor preinstall account", out)
            else:
                self.assertEqual(rc, 0, "%s: %s should pass (%s)"
                                 % (name, label, out))

    def test_guard_is_present(self):
        for name in MACOS_TEMPLATES:
            self.assertIn(MACOS_START, _render(name))

    def test_clean_root_admin_only_passes(self):
        self._check("GroupMembership: root admin dev", False,
                    "root/admin/dev are baseline admins on macos")

    def test_empty_membership_passes(self):
        self._check("", False, "empty membership")

    def test_vendor_in_admin_fails(self):
        self._check("GroupMembership: root admin foresight",
                    True, "vendor account in admin")


if __name__ == "__main__":
    unittest.main()
