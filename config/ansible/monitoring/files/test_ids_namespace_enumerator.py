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


class Collect(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            for name in filenames:
                os.unlink(os.path.join(dirpath, name))
            for name in dirnames:
                os.rmdir(os.path.join(dirpath, name))
        os.rmdir(self.root)

    def test_pairs_namespace_with_cgroup(self):
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 11, 4026532001, "0::/lxc.payload.tenant/init.scope")
        fake_proc(self.root, 12, 4026532002, "0::/lxc.payload.other/init.scope")
        mapping = ns.collect(self.root, host_inum=4026531836)
        self.assertEqual(
            mapping,
            {
                4026532001: "0::/lxc.payload.tenant/init.scope",
                4026532002: "0::/lxc.payload.other/init.scope",
            },
        )

    def test_skips_the_host_namespace(self):
        fake_proc(self.root, 1, 4026531836, "0::/init.scope")
        fake_proc(self.root, 10, 4026532001, "0::/lxc.payload.tenant/init.scope")
        self.assertEqual(list(ns.collect(self.root, host_inum=4026531836)), [4026532001])

    def test_skips_processes_without_a_namespace_symlink(self):
        fake_proc(self.root, 10, None, "0::/lxc.payload.tenant/init.scope")
        self.assertEqual(ns.collect(self.root, host_inum=4026531836), {})

    def test_missing_proc_root_publishes_nothing(self):
        self.assertEqual(ns.collect(os.path.join(self.root, "absent"), 1), {})


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


if __name__ == "__main__":
    unittest.main()
