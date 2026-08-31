"""Tests for node-type detection in the admin app."""

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for _path in (_HERE, _FILES, os.path.join(_FILES, 'ycluster')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import app


class CurrentNodeTypeTests(unittest.TestCase):
    def test_prefixes_map_to_node_types(self):
        for hostname, expected in [('storage-one', 'storage'),
                                   ('compute-one', 'compute'),
                                   ('macos-one', 'macos')]:
            with mock.patch.object(app.platform, 'node', return_value=hostname):
                self.assertEqual(app.get_current_node_type(), expected)

    def test_unknown_and_empty_hostnames(self):
        for hostname in ('node-x', ''):
            with mock.patch.object(app.platform, 'node', return_value=hostname):
                self.assertEqual(app.get_current_node_type(), 'unknown')


if __name__ == '__main__':
    unittest.main()
