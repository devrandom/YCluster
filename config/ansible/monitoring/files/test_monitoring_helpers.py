#!/usr/bin/env python3
"""Unit tests for OSV snapshot publishing and VM GPU metric collection."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent


def load_module(filename, name):
    spec = importlib.util.spec_from_file_location(
        name, _HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = load_module('osv-feed-publish.py', 'osv_feed_publish')
gpu = load_module('vm-gpu-collector.py', 'vm_gpu_collector')


class OSVPublishTests(unittest.TestCase):
    def test_atomic_copy_replaces_destination(self):
        with tempfile.TemporaryDirectory() as src_dir, \
                tempfile.TemporaryDirectory() as dst_dir:
            src = Path(src_dir) / 'osv-feed.sqlite'
            dst = Path(dst_dir) / 'osv-feed.sqlite'
            src.write_text('new')
            dst.write_text('old')
            publish.atomic_copy(str(src), str(dst_dir))
            self.assertEqual(dst.read_text(), 'new')
            self.assertEqual(dst.stat().st_mode & 0o777, 0o644)
            self.assertFalse(list(Path(dst_dir).glob('.osv-snap-*')))

    def test_main_skips_missing_files_and_publishes_present_files(self):
        with tempfile.TemporaryDirectory() as src_dir, \
                tempfile.TemporaryDirectory() as dst_dir:
            (Path(src_dir) / 'epss.json').write_text('{}')
            with mock.patch.object(publish, 'SRC', src_dir), \
                    mock.patch.object(publish, 'DEST', dst_dir), \
                    mock.patch.object(publish.sys, 'stderr') as err:
                self.assertEqual(publish.main(), 0)
            names = [str(call) for call in err.mock_calls]
            self.assertTrue(any('skip osv-feed.sqlite' in x for x in names))
            self.assertTrue(any('published epss.json' in x for x in names))
            self.assertEqual(
                (Path(dst_dir) / 'epss.json').read_text(), '{}')


class RunningVMTests(unittest.TestCase):
    def test_list_running_vms_filters_and_handles_errors(self):
        response = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([
                {'name': 'vm1', 'status': 'Running'},
                {'name': 'vm2', 'status': 'Stopped'},
            ]), stderr='')
        with mock.patch.object(gpu.subprocess, 'run', return_value=response):
            self.assertEqual(gpu.list_running_vms(), ['vm1'])

        with mock.patch.object(gpu.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(
                                   'incus', 5)), \
                mock.patch.object(gpu.sys, 'stderr'):
            self.assertEqual(gpu.list_running_vms(), [])

        response = subprocess.CompletedProcess([], 1, stdout='', stderr='no')
        with mock.patch.object(gpu.subprocess, 'run', return_value=response), \
                mock.patch.object(gpu.sys, 'stderr'):
            self.assertEqual(gpu.list_running_vms(), [])

        response = subprocess.CompletedProcess([], 0, stdout='bad', stderr='')
        with mock.patch.object(gpu.subprocess, 'run', return_value=response), \
                mock.patch.object(gpu.sys, 'stderr'):
            self.assertEqual(gpu.list_running_vms(), [])

    def test_query_vm_timeout_and_bad_rows(self):
        with mock.patch.object(gpu.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired(
                                   'incus', 5)), \
                mock.patch.object(gpu.sys, 'stderr') as err:
            self.assertEqual(gpu.query_vm('vm1'), [])
            self.assertIn('timed out', str(err.mock_calls))

        response = subprocess.CompletedProcess([], 0, stdout='''
            0,pci0,NVIDIA,10,20,1024,2048,50,30
            bad,line
            1,pci1,NVIDIA,not-a-number,20,1024,2048,50,30
            2,pci2,NVIDIA,20,30,1024,2048,50,31
        ''', stderr='')
        with mock.patch.object(gpu.subprocess, 'run', return_value=response):
            self.assertEqual(len(gpu.query_vm('vm1')), 2)

    def test_query_vm_ignores_nonzero_return(self):
        response = subprocess.CompletedProcess([], 1, stdout='', stderr='no')
        with mock.patch.object(gpu.subprocess, 'run', return_value=response):
            self.assertEqual(gpu.query_vm('vm1'), [])

    def test_write_prom_and_main(self):
        sample = ('vm1', {
            'index': '0', 'pci_bus_id': 'pci0', 'name': 'NVIDIA',
            'util_gpu': 1.0, 'util_mem': 2.0, 'mem_used_mib': 3.0,
            'mem_total_mib': 4.0, 'temp_c': 5.0, 'power_w': 6.0,
        })
        out_path = Path(tempfile.gettempdir()) / 'vm_gpu.prom'
        original_out = gpu.OUT
        gpu.OUT = str(out_path)
        try:
            gpu.write_prom([sample])
            body = out_path.read_text()
            self.assertIn('node_vm_gpu_utilization_percent', body)
            self.assertIn('vm="vm1"', body)
            self.assertIn('} 4194304.0', body)

            with mock.patch.object(gpu, 'list_running_vms',
                                   return_value=['vm1']), \
                    mock.patch.object(gpu, 'query_vm', return_value=[sample[1]]):
                self.assertEqual(gpu.main(), 0)
        finally:
            gpu.OUT = original_out
            if out_path.exists():
                out_path.unlink()


if __name__ == '__main__':
    unittest.main()
