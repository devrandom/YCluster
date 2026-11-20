"""Unit tests for the staged (atomic) guest file push.

`incus file push` truncates the destination before writing, so a push that
fails part-way (a full guest filesystem, a dying agent) leaves a truncated
file behind and still reports success. `_push_file` therefore stages the
content beside the target and only renames it into place once the guest
reports the full length.
"""

import unittest
from unittest import mock

from ycluster.utils import vm_manager as vm

PATH = "/home/ubuntu/.ssh/authorized_keys"
STAGED = PATH + ".new"
CONTENT = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample dev@desk\n"


class FakeIncus:
    def __init__(self, staged_size):
        self.staged_size = staged_size
        self.calls = []

    def __call__(self, *args, check=True, stdin=None, timeout=None):
        self.calls.append(args)
        if args[:4] == ("exec", "vm1", "--", "stat"):
            return mock.Mock(returncode=0, stdout=f"{self.staged_size}\n")
        return mock.Mock(returncode=0, stdout="")

    def ran(self, *args):
        return any(call == args for call in self.calls)


class PushFileTests(unittest.TestCase):
    def _run(self, staged_size, content=CONTENT):
        incus = FakeIncus(staged_size)
        with mock.patch.object(vm, "_incus", incus), \
             mock.patch.object(vm.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stderr="")
            vm._push_file("vm1", PATH, content, "ubuntu")
            return incus, run

    def test_pushes_to_staged_path_then_renames(self):
        incus, run = self._run(len(CONTENT.encode()))
        self.assertEqual(run.call_args[0][0][4], f"vm1{STAGED}")
        self.assertTrue(incus.ran("exec", "vm1", "--", "mv", "-f", STAGED, PATH))

    def test_never_pushes_straight_to_target(self):
        _, run = self._run(len(CONTENT.encode()))
        self.assertNotEqual(run.call_args[0][0][4], f"vm1{PATH}")

    def test_short_write_leaves_target_untouched(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(0)
        self.assertIn("left " + PATH + " untouched", str(ctx.exception))

    def test_short_write_discards_staged_file(self):
        incus = FakeIncus(0)
        with mock.patch.object(vm, "_incus", incus), \
             mock.patch.object(vm.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="")):
            with self.assertRaises(RuntimeError):
                vm._push_file("vm1", PATH, CONTENT, "ubuntu")
        self.assertTrue(incus.ran("exec", "vm1", "--", "rm", "-f", STAGED))
        self.assertFalse(incus.ran("exec", "vm1", "--", "mv", "-f", STAGED, PATH))

    def test_failed_push_raises(self):
        with mock.patch.object(vm, "_incus") as incus, \
             mock.patch.object(vm.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stderr="boom")):
            with self.assertRaises(RuntimeError) as ctx:
                vm._push_file("vm1", PATH, CONTENT, "ubuntu")
        self.assertIn("boom", str(ctx.exception))
        self.assertFalse(
            any(call[:4] == ("exec", "vm1", "--", "mv") for call in incus.call_args_list))

    def test_multibyte_content_sized_in_bytes(self):
        body = "ssh-ed25519 AAAA key\u00e9\n"
        incus, _ = self._run(len(body.encode()), content=body)
        self.assertTrue(incus.ran("exec", "vm1", "--", "mv", "-f", STAGED, PATH))


if __name__ == "__main__":
    unittest.main()
