"""Tests for collect-model-stats log aggregation."""

import unittest

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for _p in (_HERE, _FILES, os.path.join(_FILES, 'ycluster')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import types

# Stub psycopg2 (module-level import; not used by the aggregation helpers).
psycopg2 = types.ModuleType('psycopg2')
psycopg2.connect = lambda *a, **k: None
sys.modules['psycopg2'] = psycopg2
_HERE = os.path.dirname(os.path.abspath(__file__))
_STATSDIR = os.path.join(_HERE, '..')
_stats_path = os.path.join(_STATSDIR, 'collect-model-stats.py')
_spec = importlib.util.spec_from_file_location('collect_model_stats', _stats_path)
_stats = importlib.util.module_from_spec(_spec)
sys.modules['collect_model_stats'] = _stats
_spec.loader.exec_module(_stats)
from collect_model_stats import _bucket_entry, _parse_entry_timestamp, aggregate_logs

_mon_path = os.path.join(_FILES, '..', '..', 'monitoring', 'files', 'node-temps-collector.py')
_mon_spec = importlib.util.spec_from_file_location('node_temps_collector', _mon_path)
_mon = importlib.util.module_from_spec(_mon_spec)
sys.modules['node_temps_collector'] = _mon
_mon_spec.loader.exec_module(_mon)


class ParseTimestampTests(unittest.TestCase):
    def test_valid_iso(self):
        ts = _parse_entry_timestamp({'time': '2026-08-30T10:00:00Z'})
        self.assertIsNotNone(ts)

    def test_missing_time(self):
        self.assertIsNone(_parse_entry_timestamp({}))

    def test_bad_time(self):
        self.assertIsNone(_parse_entry_timestamp({'time': 'not-a-date'}))


class BucketEntryTests(unittest.TestCase):
    def test_accumulates(self):
        buckets = {}
        entry = {'user': 'a', 'model': 'm', 'duration_ms': 100,
                 'bytes_out': 50, 'time': '2026-08-30T10:00:00Z'}
        _bucket_entry(buckets, entry, 1000.0)
        _bucket_entry(buckets, entry, 1000.0)
        ((_, _, user, model), stats), = buckets.items()
        self.assertEqual(user, 'a')
        self.assertEqual(model, 'm')
        self.assertEqual(stats['request_count'], 2)
        self.assertEqual(stats['total_duration_ms'], 200)
        self.assertEqual(stats['total_bytes_out'], 100)


class AggregateLogsTests(unittest.TestCase):
    def test_skips_missing_model(self):
        result = aggregate_logs([{'time': '2026-08-30T10:00:00Z'}])
        self.assertEqual(result, [])

    def test_skips_bad_timestamp(self):
        result = aggregate_logs([{'model': 'm', 'time': 'bad'}])
        self.assertEqual(result, [])

    def test_groups_by_user_model(self):
        entries = [
            {'user': 'a', 'model': 'm', 'time': '2026-08-30T10:00:00Z',
             'duration_ms': 10, 'bytes_out': 1},
            {'user': 'b', 'model': 'm', 'time': '2026-08-30T10:00:00Z',
             'duration_ms': 20, 'bytes_out': 2},
        ]
        result = aggregate_logs(entries)
        self.assertEqual(len(result), 2)
