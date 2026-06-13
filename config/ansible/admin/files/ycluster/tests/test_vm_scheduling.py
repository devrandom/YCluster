"""Unit tests for the VM scheduling capacity logic in vm_manager.

Pure functions only (no etcd / no incus): window parsing, desired-state
evaluation, and the GPU commitment / conflict model that backs the
schedule page's save-time admission control.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest discover tests
or a single module:
    python3 -m unittest tests.test_vm_scheduling
"""

import unittest
from datetime import datetime, timedelta, timezone

from ycluster.utils import vm_manager as vm

UTC = timezone.utc


def dt(y=2026, mo=6, d=12, h=0, mi=0, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


def win(start, end):
    """A window dict as stored in etcd / sent by the page."""
    return {"start": start.isoformat(), "end": end.isoformat()}


class ParseWindowTests(unittest.TestCase):
    def test_valid_window(self):
        s, e = dt(h=10), dt(h=14)
        self.assertEqual(vm._parse_window(win(s, e)), (s, e))

    def test_naive_timezone_rejected(self):
        # The page always sends explicit offsets; a naive datetime is a bug.
        self.assertIsNone(vm._parse_window(
            {"start": "2026-06-12T10:00:00", "end": "2026-06-12T14:00:00"}))

    def test_end_before_start_rejected(self):
        self.assertIsNone(vm._parse_window(win(dt(h=14), dt(h=10))))

    def test_zero_length_rejected(self):
        self.assertIsNone(vm._parse_window(win(dt(h=10), dt(h=10))))

    def test_malformed_rejected(self):
        self.assertIsNone(vm._parse_window({"start": "nope", "end": "nope"}))
        self.assertIsNone(vm._parse_window({}))
        self.assertIsNone(vm._parse_window({"start": None, "end": None}))

    def test_elapsed_dropped_only_with_now(self):
        s, e = dt(h=10), dt(h=14)
        # Without `now`, a past window still parses (used by _desired_on,
        # which judges containment itself).
        self.assertEqual(vm._parse_window(win(s, e)), (s, e))
        # With `now` past the end, it's dropped (admission/commitments).
        self.assertIsNone(vm._parse_window(win(s, e), now=dt(h=20)))
        # `now` inside the window keeps it.
        self.assertEqual(vm._parse_window(win(s, e), now=dt(h=12)), (s, e))

    def test_offset_normalized_to_utc(self):
        # +02:00 10:00 == 08:00 UTC.
        plus2 = timezone(timedelta(hours=2))
        s, e = vm._parse_window(win(dt(h=10, tz=plus2), dt(h=14, tz=plus2)))
        self.assertEqual(s, dt(h=8))
        self.assertEqual(e, dt(h=12))
        self.assertEqual(s.tzinfo, UTC)


class DesiredOnTests(unittest.TestCase):
    def test_on_off(self):
        self.assertTrue(vm._desired_on({"mode": "on"}, dt(h=12)))
        self.assertFalse(vm._desired_on({"mode": "off"}, dt(h=12)))

    def test_schedule_in_and_out_of_window(self):
        d = {"mode": "schedule", "windows": [win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm._desired_on(d, dt(h=12)))
        self.assertFalse(vm._desired_on(d, dt(h=9)))
        # Half-open: end is exclusive, start inclusive.
        self.assertTrue(vm._desired_on(d, dt(h=10)))
        self.assertFalse(vm._desired_on(d, dt(h=14)))

    def test_multiple_windows_any_match(self):
        d = {"mode": "schedule", "windows": [win(dt(h=2), dt(h=4)),
                                             win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm._desired_on(d, dt(h=11)))
        self.assertFalse(vm._desired_on(d, dt(h=6)))

    def test_malformed_window_ignored(self):
        d = {"mode": "schedule", "windows": [{"start": "x", "end": "y"},
                                             win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm._desired_on(d, dt(h=12)))

    def test_no_windows_is_off(self):
        self.assertFalse(vm._desired_on({"mode": "schedule", "windows": []},
                                        dt(h=12)))


def vmrec(host="nv2", gpus=2, owner="a@x", type="vm"):
    return {"host": host, "gpus": gpus, "owner": owner, "type": type}


class CommitmentsTests(unittest.TestCase):
    NOW = dt(h=12)

    def test_unmanaged_holds_gpus_always(self):
        # No desired record -> unmanaged -> always-held (windows None).
        c = vm.gpu_commitments({"v": vmrec()}, {}, self.NOW)
        self.assertEqual(len(c["nv2"]), 1)
        self.assertIsNone(c["nv2"][0]["windows"])
        self.assertEqual(c["nv2"][0]["gpus"], 2)
        self.assertEqual(c["nv2"][0]["mode"], "unmanaged")

    def test_on_holds_always(self):
        c = vm.gpu_commitments({"v": vmrec()}, {"v": {"mode": "on"}}, self.NOW)
        self.assertIsNone(c["nv2"][0]["windows"])

    def test_off_holds_nothing(self):
        c = vm.gpu_commitments({"v": vmrec()}, {"v": {"mode": "off"}}, self.NOW)
        self.assertEqual(c, {})

    def test_schedule_holds_during_live_windows(self):
        d = {"v": {"mode": "schedule",
                   "windows": [win(dt(h=14), dt(h=16))]}}
        c = vm.gpu_commitments({"v": vmrec()}, d, self.NOW)
        self.assertEqual(c["nv2"][0]["windows"], [(dt(h=14), dt(h=16))])

    def test_schedule_with_only_elapsed_windows_omitted(self):
        d = {"v": {"mode": "schedule",
                   "windows": [win(dt(h=8), dt(h=10))]}}  # ended before NOW
        self.assertEqual(vm.gpu_commitments({"v": vmrec()}, d, self.NOW), {})

    def test_container_never_counted(self):
        c = vm.gpu_commitments({"v": vmrec(type="container")}, {}, self.NOW)
        self.assertEqual(c, {})

    def test_zero_gpu_and_hostless_skipped(self):
        reg = {"a": vmrec(gpus=0), "b": vmrec(host=None)}
        self.assertEqual(vm.gpu_commitments(reg, {}, self.NOW), {})

    def test_owner_carried_through(self):
        c = vm.gpu_commitments({"v": vmrec(owner="bob@x")}, {}, self.NOW)
        self.assertEqual(c["nv2"][0]["owner"], "bob@x")

    def test_groups_by_host(self):
        reg = {"a": vmrec(host="nv2"), "b": vmrec(host="nv3"),
               "c": vmrec(host="nv2")}
        c = vm.gpu_commitments(reg, {}, self.NOW)
        self.assertEqual(sorted(c), ["nv2", "nv3"])
        self.assertEqual(len(c["nv2"]), 2)


def commit(gpus=2, windows=None):
    return {"vm": "x", "owner": "a@x", "gpus": gpus,
            "mode": "schedule" if windows else "on", "windows": windows}


class ConflictTests(unittest.TestCase):
    def test_fits_in_empty_pool(self):
        self.assertIsNone(vm.gpu_conflict([], 2, 3, dt(h=10), dt(h=14)))

    def test_always_held_overcommit_detected_at_lo(self):
        # 2 held continuously, pool 3, candidate wants 2 -> only 1 free.
        worst = vm.gpu_conflict([commit(2)], 2, 3, dt(h=10), dt(h=14))
        self.assertIsNotNone(worst)
        when, free, holders = worst
        self.assertEqual(when, dt(h=10))
        self.assertEqual(free, 1)
        self.assertEqual(len(holders), 1)

    def test_future_window_start_is_a_probe_point(self):
        # Nothing held at lo, but a 2-GPU window opens at 12 inside the
        # candidate's [10,14) range -> conflict surfaces at 12, not 10.
        c = commit(2, [(dt(h=12), dt(h=16))])
        worst = vm.gpu_conflict([c], 2, 3, dt(h=10), dt(h=14))
        self.assertIsNotNone(worst)
        self.assertEqual(worst[0], dt(h=12))

    def test_disjoint_windows_fit(self):
        # Existing holds 14-16; candidate wants 10-14 -> no overlap.
        c = commit(2, [(dt(h=14), dt(h=16))])
        self.assertIsNone(vm.gpu_conflict([c], 2, 3, dt(h=10), dt(h=14)))

    def test_picks_worst_point(self):
        # One window holds 1 GPU from 10, a second holds 2 more from 12;
        # the peak (least free) is at 12.
        commits = [commit(1, [(dt(h=10), dt(h=18))]),
                   commit(2, [(dt(h=12), dt(h=18))])]
        worst = vm.gpu_conflict(commits, 1, 3, dt(h=10), dt(h=16))
        self.assertEqual(worst[0], dt(h=12))
        self.assertEqual(worst[1], 0)            # 3 - 1 - 2

    def test_exactly_fits_is_not_a_conflict(self):
        # pool 3, 1 held, candidate wants 2 -> exactly fits.
        self.assertIsNone(vm.gpu_conflict([commit(1)], 2, 3,
                                          dt(h=10), dt(h=14)))


if __name__ == "__main__":
    unittest.main()
