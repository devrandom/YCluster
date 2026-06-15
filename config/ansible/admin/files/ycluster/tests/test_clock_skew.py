"""Unit tests for ycluster.utils.clock_skew (node-type clock-skew bands).

Loaded by file path so it runs without the package __init__ (which imports
etcd3, a cluster-only dep); this logic is pure arithmetic.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest tests.test_clock_skew
"""

import importlib.util
import pathlib
import unittest

_MOD = pathlib.Path(__file__).resolve().parents[1] / "ycluster" / "utils" / "clock_skew.py"
_spec = importlib.util.spec_from_file_location("clock_skew", _MOD)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


class StorageBandTests(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(cs.classify_clock_offset(50, 'storage'), 'healthy')
        self.assertEqual(cs.classify_clock_offset(100, 'storage'), 'healthy')

    def test_warning(self):
        self.assertEqual(cs.classify_clock_offset(290, 'storage'), 'warning')
        self.assertEqual(cs.classify_clock_offset(1000, 'storage'), 'warning')

    def test_critical(self):
        self.assertEqual(cs.classify_clock_offset(1001, 'storage'), 'critical')


class NonStorageBandTests(unittest.TestCase):
    def test_290ms_is_healthy(self):
        # The m3 case that prompted the looser band.
        for node_type in ('compute', 'macos', 'nvidia', 'unknown'):
            self.assertEqual(cs.classify_clock_offset(290, node_type), 'healthy')

    def test_boundary(self):
        self.assertEqual(cs.classify_clock_offset(1000, 'compute'), 'healthy')
        self.assertEqual(cs.classify_clock_offset(1001, 'compute'), 'warning')
        self.assertEqual(cs.classify_clock_offset(10000, 'compute'), 'warning')
        self.assertEqual(cs.classify_clock_offset(10001, 'compute'), 'critical')


class SignAndThresholdTests(unittest.TestCase):
    def test_negative_offset_uses_magnitude(self):
        self.assertEqual(cs.classify_clock_offset(-1500, 'storage'), 'critical')
        self.assertEqual(cs.classify_clock_offset(-1500, 'compute'), 'warning')

    def test_thresholds(self):
        self.assertEqual(cs.clock_skew_thresholds('storage'), (100, 1000))
        self.assertEqual(cs.clock_skew_thresholds('compute'), (1000, 10000))


if __name__ == '__main__':
    unittest.main()
