#!/usr/bin/env python3
"""Unit tests for blackbox target and usage-stats provisioning helpers."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
import sys
PACKAGE_ROOT = ROOT / 'config' / 'ansible' / 'admin' / 'files' / 'ycluster'
for path in (ROOT, PACKAGE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bb = load_module('update-blackbox-targets.py', 'update_blackbox_targets')


class UplinkHelperTests(unittest.TestCase):
    def test_has_internet_uplink(self):
        responses = [
            SimpleNamespace(returncode=0, stdout='[{"dev":"eth0"}]',
                            stderr=''),
            SimpleNamespace(returncode=0, stdout='[]', stderr=''),
            SimpleNamespace(returncode=1, stdout='[]', stderr='no route'),
        ]
        for response, expected in zip(responses, [True, False, False]):
            with mock.patch.object(bb.subprocess, 'run',
                                   return_value=response):
                self.assertEqual(bb.has_internet_uplink(), expected)
        with mock.patch.object(bb.subprocess, 'run',
                               side_effect=RuntimeError('boom')):
            self.assertFalse(bb.has_internet_uplink())

    def test_get_targets_count(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing.json'
            bb.TARGETS_FILE = str(missing)
            self.assertEqual(bb.get_targets_count(), 0)
            bad = Path(directory) / 'bad.json'
            bad.write_text('not json')
            bb.TARGETS_FILE = str(bad)
            self.assertEqual(bb.get_targets_count(), 0)
            good = Path(directory) / 'targets.json'
            good.write_text('[1, 2, 3]')
            bb.TARGETS_FILE = str(good)
            self.assertEqual(bb.get_targets_count(), 3)

    def test_get_https_domain(self):
        client = SimpleNamespace(
            get=lambda key: (b' example.test ', None))
        with mock.patch.object(bb, 'get_etcd_client', return_value=client):
            self.assertEqual(bb.get_https_domain(), 'example.test')
        with mock.patch.object(bb, 'get_etcd_client',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(bb.get_https_domain())

    def test_get_blackbox_exporters(self):
        entries = [
            (json.dumps({'name': 'a', 'ip': 'proxy.test'}).encode(), None),
            (json.dumps({'name': 'b', 'hostname': 'host-b'}).encode(), None),
            (json.dumps({'name': 'ignored'}).encode(), None),
            (b'bad json', None),
        ]
        client = SimpleNamespace(get_prefix=lambda prefix: entries)
        with mock.patch.object(bb, 'get_etcd_client', return_value=client), \
                mock.patch.object(bb.os, 'uname',
                                  return_value=SimpleNamespace(
                                      nodename='core')):
            self.assertEqual(bb.get_blackbox_exporters(), [
                {'address': 'localhost:9115', 'blackbox_node': 'core',
                 'node_type': 'core'},
                {'address': 'proxy.test:9115', 'blackbox_node': 'a',
                 'node_type': 'frontend'},
                {'address': 'host-b:9115', 'blackbox_node': 'b',
                 'node_type': 'frontend'},
            ])
        with mock.patch.object(bb, 'get_etcd_client',
                               side_effect=RuntimeError('boom')):
            self.assertEqual(len(bb.get_blackbox_exporters()), 1)

    def test_probe_specs(self):
        specs = bb.get_probe_specs('example.test')
        self.assertEqual([spec['probe_name'] for spec in specs],
                         ['inference-models', 'admin-ping'])
        self.assertIn('/healthz', specs[0]['url'])
        self.assertIn('/api/ping', specs[1]['url'])


class BlackboxUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.targets = self.directory / 'targets.json'
        self.metrics = self.directory / 'uplink.prom'
        self.original = (bb.TARGETS_FILE, bb.TEXTFILE_DIR, bb.METRICS_FILE)
        bb.TARGETS_FILE = str(self.targets)
        bb.TEXTFILE_DIR = str(self.directory)
        bb.METRICS_FILE = str(self.metrics)

    def tearDown(self):
        bb.TARGETS_FILE, bb.TEXTFILE_DIR, bb.METRICS_FILE = self.original

    def test_write_uplink_metrics(self):
        with mock.patch.object(bb, 'has_internet_uplink', return_value=True), \
                mock.patch.object(bb, 'get_targets_count', return_value=2), \
                mock.patch.object(bb.shutil, 'chown') as chown:
            bb.write_uplink_metrics()
        body = self.metrics.read_text()
        self.assertIn('ycluster_uplink_status', body)
        self.assertIn('ycluster_blackbox_targets_count', body)
        chown.assert_called_once()

    def test_update_without_uplink_clears_targets(self):
        with mock.patch.object(bb, 'has_internet_uplink', return_value=False), \
                mock.patch.object(bb, 'get_https_domain',
                                  return_value='example.test'), \
                mock.patch.object(bb, 'get_blackbox_exporters',
                                  return_value=[]), \
                mock.patch.object(bb.shutil, 'chown'), \
                mock.patch.object(bb, 'write_uplink_metrics') as write:
            self.assertTrue(bb.update_blackbox_targets())
        self.assertEqual(json.loads(self.targets.read_text()), [])
        write.assert_called_once_with()

    def test_update_with_domain_writes_matrix(self):
        with mock.patch.object(bb, 'has_internet_uplink', return_value=True), \
                mock.patch.object(bb, 'get_https_domain',
                                  return_value='example.test'), \
                mock.patch.object(
                    bb, 'get_blackbox_exporters',
                    return_value=[{'address': 'localhost:9115',
                                   'blackbox_node': 'core',
                                   'node_type': 'core'}]), \
                mock.patch.object(bb.shutil, 'chown'), \
                mock.patch.object(bb, 'write_uplink_metrics') as write:
            self.assertTrue(bb.update_blackbox_targets())
        targets = json.loads(self.targets.read_text())
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]['labels']['domain'], 'example.test')
        write.assert_called_once_with()

    def test_update_without_domain_clears_targets(self):
        with mock.patch.object(bb, 'has_internet_uplink', return_value=True), \
                mock.patch.object(bb, 'get_https_domain', return_value=None), \
                mock.patch.object(bb, 'get_blackbox_exporters',
                                  return_value=[]), \
                mock.patch.object(bb.shutil, 'chown'), \
                mock.patch.object(bb, 'write_uplink_metrics') as write:
            self.assertTrue(bb.update_blackbox_targets())
        self.assertEqual(json.loads(self.targets.read_text()), [])
        write.assert_called_once_with()

    def test_update_failure_returns_false(self):
        with mock.patch.object(bb, 'has_internet_uplink', return_value=True), \
                mock.patch.object(bb, 'get_https_domain',
                                  return_value='example.test'), \
                mock.patch.object(bb, 'get_blackbox_exporters',
                                  return_value=[]), \
                mock.patch.object(bb.tempfile, 'NamedTemporaryFile',
                                  side_effect=OSError('boom')), \
                mock.patch('builtins.print') as printed:
            self.assertFalse(bb.update_blackbox_targets())
        self.assertIn('Failed to update', printed.call_args.args[0])

    def test_main(self):
        with mock.patch.object(bb, 'update_blackbox_targets',
                               return_value=True) as update:
            self.assertEqual(bb.main(), 0)
            update.assert_called_once_with()
