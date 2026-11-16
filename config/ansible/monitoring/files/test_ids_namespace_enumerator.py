#!/usr/bin/env python3
"""Unit tests for the pid-namespace enumerator.

Run: python3 -m unittest test_ids_namespace_enumerator  (from this dir)
"""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "ids-namespace-enumerator")
_LOADER = importlib.machinery.SourceFileLoader("ids_namespace_enumerator", _PATH)
_SPEC = importlib.util.spec_from_loader("ids_namespace_enumerator", _LOADER)
ns = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(ns)


def fake_proc(root, pid, inum, cgroup):
    base = os.path.join(root, str(pid))
    os.makedirs(os.path.join(base, "ns"), exist_ok=True)
    if inum is not None:
        os.symlink(f"pid:[{inum}]", os.path.join(base, "ns", "pid"))
    with open(os.path.join(base, "cgroup"), "w", encoding="utf-8") as fh:
        fh.write(cgroup + "\n")


def remove_tree(root):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            os.rmdir(os.path.join(dirpath, name))
    os.rmdir(root)


class Collect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        remove_tree(self.root)

    def test_pairs_namespace_with_cgroup(self):
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 11, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 12, 4026532002, "0::/lxc.payload.other/init.scope")
        mapping = ns.collect(self.root, host_inum=4026531836)
        self.assertEqual(
            mapping,
            {
                4026532001: ["0::/lxc.payload.tenant/init.scope"],
                4026532002: ["0::/lxc.payload.other/init.scope"],
            },
        )

    def test_skips_the_host_namespace(self):
        fake_proc(self.root, 1, 4026531836, "0::/init.scope")
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        self.assertEqual(list(ns.collect(self.root, host_inum=4026531836)), [4026532001])

    def test_skips_processes_without_a_namespace_symlink(self):
        fake_proc(self.root, 10, None, "0::/lxc.payload.tenant/init.scope")
        self.assertEqual(ns.collect(self.root, host_inum=4026531836), {})

    def test_keeps_every_cgroup_text_of_a_namespace(self):
        # One container runs a service per cgroup: publishing a single text
        # would be an arbitrary pick, and the forwarder needs to see both when
        # a second runtime shares the namespace.
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 11, 4026532001, "0::/lxc.payload.tenant/system.slice/ssh.service")
        fake_proc(self.root, 12, 4026532001, "0::/lxc.payload.tenant/system.slice/ssh.service")
        self.assertEqual(
            ns.collect(self.root, host_inum=4026531836),
            {4026532001: [
                "0::/lxc.payload.tenant/init.scope",
                "0::/lxc.payload.tenant/system.slice/ssh.service",
            ]},
        )

    def test_bounds_the_texts_published_per_namespace(self):
        # A tenant controls how many cgroups its container sees, so the file
        # must not grow with them.
        for pid in range(10, 10 + ns.MAX_TEXTS_PER_NAMESPACE + 20):
            fake_proc(self.root, pid, 4026532001, f"0::/lxc.payload.tenant/scope-{pid}.scope")
        texts = ns.collect(self.root, host_inum=4026531836)[4026532001]
        self.assertEqual(len(texts), ns.MAX_TEXTS_PER_NAMESPACE)
        self.assertEqual(texts, sorted(texts))

    def test_unreadable_proc_root_raises(self):
        # An unreadable procfs must fail the unit instead of publishing an
        # empty map that looks like a host without containers.
        with self.assertRaises(FileNotFoundError):
            ns.collect(os.path.join(self.root, "absent"), 1)

    def test_malformed_namespace_links_are_ignored(self):
        fake_proc(self.root, 10, None, "0::/lxc.payload.tenant/init.scope")
        os.symlink("net:[4026532001]", os.path.join(self.root, "10", "ns", "pid"))
        fake_proc(self.root, 11, None, "0::/lxc.payload.tenant/init.scope")
        os.symlink("pid:[nonsense]", os.path.join(self.root, "11", "ns", "pid"))
        self.assertEqual(ns.collect(self.root, host_inum=4026531836), {})

    def test_a_refused_namespace_is_tolerated_while_others_read(self):
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 11, 4026532002, "0::/lxc.payload.other/init.scope")
        real = ns.read_pid_namespace_inum

        def deny_one(path):
            if "/10/" in path or path.endswith("/10/ns/pid"):
                raise PermissionError("denied")
            return real(path)

        with mock.patch.object(ns, "read_pid_namespace_inum", deny_one):
            self.assertEqual(ns.collect(self.root, host_inum=4026531836),
                             {4026532002: ["0::/lxc.payload.other/init.scope"]})

    def test_all_refusals_raise(self):
        # Losing the ptrace access the unit exists for looks the same as a
        # host without containers unless it is allowed to fail.
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        with mock.patch.object(ns, "read_pid_namespace_inum",
                               side_effect=PermissionError("denied")):
            with self.assertRaises(RuntimeError):
                ns.collect(self.root, host_inum=4026531836)

    def test_unreadable_cgroups_are_tolerated_while_others_read(self):
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 11, 4026532002, "0::/lxc.payload.other/init.scope")
        os.unlink(os.path.join(self.root, "11", "cgroup"))
        self.assertEqual(ns.collect(self.root, host_inum=4026531836),
                         {4026532001: ["0::/lxc.payload.tenant/init.scope"]})

    def test_no_readable_cgroup_raises(self):
        # Container processes were seen, so an empty map would be a lie.
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        os.unlink(os.path.join(self.root, "10", "cgroup"))
        with self.assertRaises(RuntimeError):
            ns.collect(self.root, host_inum=4026531836)

    def test_text_length_is_capped(self):
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/" + "x" * 4000)
        texts = ns.collect(self.root, host_inum=4026531836)[4026532001]
        self.assertEqual(len(texts[0]), ns.MAX_TEXT_BYTES)

    def test_map_is_kept_under_the_byte_budget(self):
        # A tenant chooses how many pid namespaces it creates and how long its
        # cgroup paths are; what the reader refuses must not be what we write.
        for n in range(20):
            for pid in range(100, 100 + ns.MAX_TEXTS_PER_NAMESPACE):
                fake_proc(self.root, pid + n * 1000, 4026533000 + n,
                          f"0::/lxc.payload.tenant{n}/" + "s" * 200 + f"-{pid}.scope")
        with mock.patch.object(ns, "MAX_MAP_BYTES", 8192):
            mapping = ns.collect(self.root, host_inum=4026531836)
        body = json.dumps({str(k): v for k, v in mapping.items()}, sort_keys=True)
        self.assertLessEqual(len(body), 8192)
        self.assertEqual(list(mapping), sorted(mapping))
        self.assertIn(4026533000, mapping)


class Main(unittest.TestCase):
    def test_refuses_to_publish_without_a_host_namespace(self):
        root = tempfile.mkdtemp()
        out = os.path.join(root, "namespaces.json")
        argv = ["ids-namespace-enumerator", "--proc-root", root, "--output", out]
        with mock.patch("sys.argv", argv):
            with self.assertRaises(SystemExit) as raised:
                ns.main()
        self.assertNotEqual(raised.exception.code, 0)
        self.assertFalse(os.path.exists(out))
        remove_tree(root)

    def test_publishes_the_map(self):
        root = tempfile.mkdtemp()
        fake_proc(root, "self", 4026531836, "0::/init.scope")
        fake_proc(root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        out = os.path.join(root, "namespaces.json")
        argv = ["ids-namespace-enumerator", "--proc-root", root, "--output", out]
        with mock.patch("sys.argv", argv):
            ns.main()
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh),
                             {"4026532001": ["0::/lxc.payload.tenant/init.scope"]})
        remove_tree(root)


class WriteMap(unittest.TestCase):
    def test_writes_sorted_json_with_world_readable_mode(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "namespaces.json")
        ns.write_map(path, {4026532002: "b", 4026532001: "a"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"4026532001": "a", "4026532002": "b"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(root)), ["namespaces.json"])
        os.unlink(path)
        os.rmdir(root)

    def test_replaces_an_existing_map(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "namespaces.json")
        ns.write_map(path, {1: "old"})
        ns.write_map(path, {2: "new"})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"2": "new"})
        os.unlink(path)
        os.rmdir(root)

    def test_missing_directory_is_an_error(self):
        # The directory is provisioned by the unit's ReadWritePaths; creating
        # it here would hand it to root behind the forwarder's back.
        root = tempfile.mkdtemp()
        with self.assertRaises(FileNotFoundError):
            ns.write_map(os.path.join(root, "absent", "namespaces.json"), {1: "x"})
        remove_tree(root)

    def test_failed_replace_leaves_no_temporary_file(self):
        root = tempfile.mkdtemp()
        path = os.path.join(root, "namespaces.json")
        with mock.patch.object(ns.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ns.write_map(path, {1: "x"})
        self.assertEqual(os.listdir(root), [])
        remove_tree(root)


if __name__ == "__main__":
    unittest.main()
