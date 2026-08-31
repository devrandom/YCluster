"""Tests for local-node interface discovery."""

import subprocess
import unittest
from unittest import mock

from ycluster.utils import populate_local_node


def run(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')


class GetPrimaryInterfaceTests(unittest.TestCase):
    def test_default_route_interface(self):
        with mock.patch.object(populate_local_node.subprocess, 'run',
                               return_value=run('default via 10.0.0.254 dev eth0')):
            self.assertEqual(populate_local_node.get_primary_interface(), 'eth0')

    def test_cluster_address_fallback(self):
        calls = [run(''), run('2: eth0    inet 10.0.0.1/24\n3: lo inet 127.0.0.1/8')]
        with mock.patch.object(populate_local_node.subprocess, 'run',
                               side_effect=calls):
            self.assertEqual(populate_local_node.get_primary_interface(), 'eth0')

    def test_no_interface(self):
        calls = [run(''), run('3: lo inet 127.0.0.1/8')]
        with mock.patch.object(populate_local_node.subprocess, 'run',
                               side_effect=calls):
            self.assertIsNone(populate_local_node.get_primary_interface())


if __name__ == '__main__':
    unittest.main()
