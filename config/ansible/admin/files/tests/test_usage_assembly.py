"""Tests for app.py usage-interval assembly and row rendering."""

import unittest
from datetime import datetime, timezone

import app as appmod


def ts(h):
    return datetime(2026, 8, 30, h, 0, 0, tzinfo=timezone.utc)


class AssembleIntervalsTests(unittest.TestCase):
    def test_open_close_interval(self):
        since = ts(0)
        now = ts(10)
        events = [
            (ts(1), 'vm1', 'launch', 'a@x', 2, True),
            (ts(5), 'vm1', 'stop', 'a@x', 0, False),
        ]
        result = appmod._assemble_intervals(events, since, now)
        st = result['vm1']
        self.assertIsNone(st['open'])
        self.assertAlmostEqual(st['tracked'], 8.0)  # 4h × 2 GPUs
        self.assertAlmostEqual(st['billable'], 8.0)

    def test_open_interval_accrues_to_now(self):
        since = ts(0)
        now = ts(10)
        events = [(ts(2), 'vm1', 'start', 'a@x', 1, False)]
        result = appmod._assemble_intervals(events, since, now)
        st = result['vm1']
        self.assertIsNotNone(st['open'])
        self.assertAlmostEqual(st['tracked'], 8.0)  # 8h × 1 GPU
        self.assertAlmostEqual(st['billable'], 0.0)  # not billable

    def test_prewindow_event_sets_state(self):
        since = ts(5)
        now = ts(10)
        events = [
            (ts(1), 'vm1', 'launch', 'a@x', 4, True),
            (ts(6), 'vm1', 'stop', 'a@x', 0, False),
        ]
        result = appmod._assemble_intervals(events, since, now)
        st = result['vm1']
        # Interval from pre-window launch to stop at h6, clipped to window.
        self.assertAlmostEqual(st['tracked'], 4.0)  # 1h × 4 GPUs
        self.assertAlmostEqual(st['billable'], 4.0)

    def test_restart_keeps_interval_open(self):
        since = ts(0)
        now = ts(10)
        events = [
            (ts(1), 'vm1', 'launch', 'a@x', 1, True),
            (ts(3), 'vm1', 'restart', 'a@x', 1, True),
            (ts(8), 'vm1', 'stop', 'a@x', 0, False),
        ]
        result = appmod._assemble_intervals(events, since, now)
        self.assertAlmostEqual(result['vm1']['tracked'], 7.0)  # 1→8 continuous

    def test_owner_update_from_later_event(self):
        since = ts(0)
        now = ts(10)
        events = [
            (ts(1), 'vm1', 'launch', 'a@x', 1, True),
            (ts(5), 'vm1', 'start', 'b@x', 1, True),
        ]
        result = appmod._assemble_intervals(events, since, now)
        self.assertEqual(result['vm1']['owner'], 'b@x')


class UsageRowTests(unittest.TestCase):
    def test_full_row(self):
        row = appmod._usage_row(
            'vm1',
            {'owner': 'a@x', 'tracked': 10.0, 'billable': 5.0, 'open': None},
            {'observed': 9.0, 'samples': 20},
            {'vm1': {'gpus': 2}},
            {'vm1': {'mode': 'schedule', 'windows': [1]}},
            {'vm1': 'Running'},
        )
        self.assertEqual(row['vm'], 'vm1')
        self.assertEqual(row['owner'], 'a@x')
        self.assertEqual(row['gpus'], 2)
        self.assertEqual(row['schedule_mode'], 'schedule')
        self.assertEqual(row['current'], 'untracked')
        self.assertFalse(row['drift'])

    def test_drift_detection(self):
        row = appmod._usage_row(
            'vm1',
            {'owner': 'a@x', 'tracked': 10.0, 'billable': 0.0, 'open': None},
            {'observed': 20.0, 'samples': 0},
            {}, {}, {})
        self.assertTrue(row['drift'])

    def test_billable_current(self):
        row = appmod._usage_row(
            'vm1',
            {'owner': 'a@x', 'tracked': 0.0, 'billable': 0.0,
             'open': (ts(1), 2, True)},
            {}, {}, {}, {})
        self.assertEqual(row['current'], 'billable')


if __name__ == '__main__':
    unittest.main()
