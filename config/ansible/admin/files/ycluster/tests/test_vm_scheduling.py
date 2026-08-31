"""Unit tests for the VM scheduling capacity logic in vm_manager.

Pure functions only (no etcd / no incus): window parsing, desired-state
evaluation, and the GPU commitment / conflict model that backs the
schedule page's save-time admission control.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest discover tests
or a single module:
    python3 -m unittest tests.test_vm_scheduling
"""

import subprocess
import contextlib
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest import mock

from ycluster.utils import vm_manager as vm
from ycluster.utils import vm_alter

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
        # Without `now`, a past window still parses (used by desired_on,
        # which judges containment itself).
        self.assertEqual(vm._parse_window(win(s, e)), (s, e))
        # With `now` past the end, it's dropped (admission/commitments).
        self.assertIsNone(vm._parse_window(win(s, e), now=dt(h=20)))
        # `now` inside the window keeps it.
        self.assertEqual(vm._parse_window(win(s, e), now=dt(h=12)), (s, e))

    def test_offset_normalized_to_utc(self):
        # +02:00 10:00 == 08:00 UTC.
        plus2 = timezone(timedelta(hours=2))
        parsed = vm._parse_window(win(dt(h=10, tz=plus2), dt(h=14, tz=plus2)))
        assert parsed is not None
        s, e = parsed
        self.assertEqual(s, dt(h=8))
        self.assertEqual(e, dt(h=12))
        self.assertEqual(s.tzinfo, UTC)


class DesiredOnTests(unittest.TestCase):
    def test_on_off(self):
        self.assertTrue(vm.desired_on({"mode": "on"}, dt(h=12)))
        self.assertFalse(vm.desired_on({"mode": "off"}, dt(h=12)))

    def test_schedule_in_and_out_of_window(self):
        d = {"mode": "schedule", "windows": [win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm.desired_on(d, dt(h=12)))
        self.assertFalse(vm.desired_on(d, dt(h=9)))
        # Half-open: end is exclusive, start inclusive.
        self.assertTrue(vm.desired_on(d, dt(h=10)))
        self.assertFalse(vm.desired_on(d, dt(h=14)))

    def test_multiple_windows_any_match(self):
        d = {"mode": "schedule", "windows": [win(dt(h=2), dt(h=4)),
                                             win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm.desired_on(d, dt(h=11)))
        self.assertFalse(vm.desired_on(d, dt(h=6)))

    def test_malformed_window_ignored(self):
        d = {"mode": "schedule", "windows": [{"start": "x", "end": "y"},
                                             win(dt(h=10), dt(h=14))]}
        self.assertTrue(vm.desired_on(d, dt(h=12)))

    def test_no_windows_is_off(self):
        self.assertFalse(vm.desired_on({"mode": "schedule", "windows": []},
                                        dt(h=12)))

    def test_unmanaged_ignores_dormant_windows(self):
        # An unmanaged record may carry preserved windows; they must NOT be
        # read as on/off intent (the reconciler skips unmanaged anyway).
        d = {"mode": "unmanaged", "windows": [win(dt(h=10), dt(h=14))]}
        self.assertFalse(vm.desired_on(d, dt(h=12)))


class PendingStateTests(unittest.TestCase):
    NOW = dt(h=12)

    def test_unmanaged_and_no_record_never_pending(self):
        self.assertIsNone(vm.pending_state(None, "Running", None, self.NOW))
        self.assertIsNone(vm.pending_state({"mode": "unmanaged"}, "Stopped",
                                           None, self.NOW))

    def test_pending_start(self):
        self.assertEqual(vm.pending_state({"mode": "on"}, "Stopped", None,
                                          self.NOW), "start")

    def test_pending_stop_when_running(self):
        self.assertEqual(vm.pending_state({"mode": "off"}, "Running", None,
                                          self.NOW), "stop")

    def test_pending_stop_from_grace_when_status_lags(self):
        # status not yet 'Running' in the sample, but a grace marker is the
        # reconciler's own evidence of a running, pending-stop VM.
        self.assertEqual(vm.pending_state({"mode": "off"}, "unknown",
                                          {"warned_at": "x"}, self.NOW), "stop")

    def test_converged(self):
        self.assertIsNone(vm.pending_state({"mode": "on"}, "Running", None,
                                           self.NOW))
        self.assertIsNone(vm.pending_state({"mode": "off"}, "Stopped", None,
                                           self.NOW))


def vmrec(host: Optional[str] = "nv2", gpus=2, owner="a@x", type="vm"):
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

    def test_desired_gpus_used(self):
        d = {"v": {"mode": "schedule",
                   "windows": [win(dt(h=14), dt(h=16))], "gpus": 1}}
        c = vm.gpu_commitments({"v": vmrec(gpus=4)}, d, self.NOW)
        self.assertEqual(c["nv2"][0]["gpus"], 1)

    def test_desired_gpus_defaults_to_registered(self):
        # Old desired records without `gpus` mean "all allocated GPUs".
        d = {"v": {"mode": "schedule",
                   "windows": [win(dt(h=14), dt(h=16))]}}
        c = vm.gpu_commitments({"v": vmrec(gpus=4)}, d, self.NOW)
        self.assertEqual(c["nv2"][0]["gpus"], 4)

    def test_desired_gpus_zero_commits_nothing(self):
        # Explicit 0 GPUs runs CPU-only: no host-pool commitment at all.
        d = {"v": {"mode": "on", "gpus": 0}}
        self.assertEqual(vm.gpu_commitments({"v": vmrec(gpus=4)}, d, self.NOW), {})

    def test_desired_gpus_helper(self):
        self.assertEqual(vm.desired_gpus({"gpus": 2}, 4), 2)
        self.assertEqual(vm.desired_gpus({"gpus": 0}, 4), 0)  # explicit 0 kept
        self.assertEqual(vm.desired_gpus({}, 4), 4)            # absent -> registered
        self.assertEqual(vm.desired_gpus(None, 4), 4)


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


class GpuBlockCommitmentsTests(unittest.TestCase):
    NOW = dt(h=12)

    def block(self, host="nv2", gpus=2, mode="on", windows=None):
        b = {"host": host, "gpus": gpus, "mode": mode}
        if windows is not None:
            b["windows"] = windows
        return b

    def test_on_holds_always(self):
        c = vm.gpu_block_commitments({"blk1": self.block()}, self.NOW)
        self.assertEqual(c["nv2"][0]["gpus"], 2)
        self.assertIsNone(c["nv2"][0]["windows"])
        self.assertEqual(c["nv2"][0]["mode"], "on")
        self.assertTrue(c["nv2"][0]["is_block"])

    def test_schedule_holds_during_live_windows(self):
        b = self.block(mode="schedule", windows=[win(dt(h=14), dt(h=16))])
        c = vm.gpu_block_commitments({"blk1": b}, self.NOW)
        self.assertEqual(c["nv2"][0]["windows"], [(dt(h=14), dt(h=16))])

    def test_schedule_only_elapsed_omitted(self):
        b = self.block(mode="schedule", windows=[win(dt(h=8), dt(h=10))])
        self.assertEqual(vm.gpu_block_commitments({"blk1": b}, self.NOW), {})

    def test_zero_gpu_and_hostless_skipped(self):
        blocks = {"a": self.block(gpus=0), "b": self.block(host=None)}
        self.assertEqual(vm.gpu_block_commitments(blocks, self.NOW), {})

    def test_groups_by_host(self):
        blocks = {"a": self.block(host="nv2"), "b": self.block(host="nv3")}
        c = vm.gpu_block_commitments(blocks, self.NOW)
        self.assertEqual(sorted(c), ["nv2", "nv3"])

    def test_vm_is_block_id_owner_none(self):
        c = vm.gpu_block_commitments({"blk-1": self.block()}, self.NOW)
        self.assertEqual(c["nv2"][0]["vm"], "blk-1")
        self.assertIsNone(c["nv2"][0]["owner"])


class CombineCommitmentsTests(unittest.TestCase):
    NOW = dt(h=12)

    def test_merges_by_host_preserving_order(self):
        vms = vm.gpu_commitments({"v": vmrec()}, {"v": {"mode": "on"}}, self.NOW)
        blocks = vm.gpu_block_commitments(
            {"blk1": {"host": "nv2", "gpus": 2, "mode": "on"}}, self.NOW)
        merged = vm.combine_gpu_commitments(vms, blocks)
        self.assertEqual([c["vm"] for c in merged["nv2"]], ["v", "blk1"])
        # The block's commitment feeds gpu_conflict exactly like a VM's.
        self.assertIsNotNone(
            vm.gpu_conflict(merged["nv2"], 2, 3, self.NOW, dt(h=18)))

    def test_empty_inputs(self):
        self.assertEqual(vm.combine_gpu_commitments(), {})
        self.assertEqual(vm.combine_gpu_commitments({}), {})


class ConflictTests(unittest.TestCase):
    def test_fits_in_empty_pool(self):
        self.assertIsNone(vm.gpu_conflict([], 2, 3, dt(h=10), dt(h=14)))

    def test_always_held_overcommit_detected_at_lo(self):
        # 2 held continuously, pool 3, candidate wants 2 -> only 1 free.
        worst = vm.gpu_conflict([commit(2)], 2, 3, dt(h=10), dt(h=14))
        assert worst is not None
        when, free, holders = worst
        self.assertEqual(when, dt(h=10))
        self.assertEqual(free, 1)
        self.assertEqual(len(holders), 1)

    def test_future_window_start_is_a_probe_point(self):
        # Nothing held at lo, but a 2-GPU window opens at 12 inside the
        # candidate's [10,14) range -> conflict surfaces at 12, not 10.
        c = commit(2, [(dt(h=12), dt(h=16))])
        worst = vm.gpu_conflict([c], 2, 3, dt(h=10), dt(h=14))
        assert worst is not None
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
        assert worst is not None
        self.assertEqual(worst[0], dt(h=12))
        self.assertEqual(worst[1], 0)            # 3 - 1 - 2

    def test_exactly_fits_is_not_a_conflict(self):
        # pool 3, 1 held, candidate wants 2 -> exactly fits.
        self.assertIsNone(vm.gpu_conflict([commit(1)], 2, 3,
                                          dt(h=10), dt(h=14)))


class AvailableWindowTests(unittest.TestCase):
    """Future-window suggestions for the schedule page (gpu_available_windows)."""

    def test_empty_pool_suggests_capped_windows(self):
        wins = vm.gpu_available_windows([], 2, 4, dt(h=12), count=3)
        self.assertEqual([w["hours"] for w in wins], [6, 6, 6])
        self.assertEqual(wins[0]["start"], dt(h=13).isoformat(timespec="minutes"))
        self.assertEqual(wins[1]["start"], dt(h=19).isoformat(timespec="minutes"))
        self.assertEqual(wins[2]["start"], dt(d=13, h=1).isoformat(timespec="minutes"))

    def test_commitment_shortens_window(self):
        # 2 GPUs held 15-17 in a 3-GPU pool; candidate needs the remaining
        # 2 (exactly 1 free), so the next window runs 13-15 and skips 15+.
        commits = [commit(2, [(dt(h=15), dt(h=17))])]
        wins = vm.gpu_available_windows(commits, 2, 3, dt(h=12), count=1)
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["start"], dt(h=13).isoformat(timespec="minutes"))
        self.assertEqual(wins[0]["end"], dt(h=15).isoformat(timespec="minutes"))
        self.assertEqual(wins[0]["hours"], 2)

    def test_skips_fully_busy_start(self):
        # The whole pool is held 13-14, so the first suggestion starts at 14.
        commits = [commit(4, [(dt(h=13), dt(h=14))])]
        wins = vm.gpu_available_windows(commits, 2, 4, dt(h=12), count=1)
        self.assertEqual(wins[0]["start"], dt(h=14).isoformat(timespec="minutes"))
        self.assertEqual(wins[0]["hours"], 6)

    def test_returns_empty_when_pool_unknown_or_too_small(self):
        self.assertEqual(vm.gpu_available_windows([], 2, None, dt(h=12)), [])
        self.assertEqual(vm.gpu_available_windows([], 5, 4, dt(h=12)), [])
        self.assertEqual(vm.gpu_available_windows([], 0, 4, dt(h=12)), [])

    def test_always_held_overcommit_returns_empty(self):
        commits = [commit(4)]
        self.assertEqual(vm.gpu_available_windows(commits, 2, 4, dt(h=12)), [])

    def test_skips_suggestions_overlapping_avoided_windows(self):
        # Own window 14-15: candidate 13-19 overlaps it, so the first
        # suggestion starts after it (15), not before.
        wins = vm.gpu_available_windows([], 2, 4, dt(h=12), count=1,
                                        avoid_windows=[(dt(h=14), dt(h=15))])
        self.assertEqual(wins[0]["start"], dt(h=15).isoformat(timespec="minutes"))
        self.assertEqual(wins[0]["hours"], 6)


class DesiredConflictTests(unittest.TestCase):
    NOW = dt(h=12)

    def sched(self, *windows):
        return {"mode": "schedule", "windows": [win(s, e) for s, e in windows]}

    def test_unmanaged_never_conflicts(self):
        # Both forms of unmanaged: no record, and an explicit unmanaged
        # record (with dormant preserved windows) — neither warns.
        unmanaged_rec = {"mode": "unmanaged",
                         "windows": [win(dt(h=15), dt(h=18))]}
        for action in ("start", "stop", "restart"):
            self.assertIsNone(vm.desired_conflict(None, action, self.NOW))
            self.assertIsNone(
                vm.desired_conflict(unmanaged_rec, action, self.NOW))

    def test_stop_conflicts_when_on(self):
        self.assertIsNotNone(vm.desired_conflict({"mode": "on"}, "stop", self.NOW))
        # in-window schedule also wants ON
        d = self.sched((dt(h=10), dt(h=14)))
        conflict = vm.desired_conflict(d, "stop", self.NOW)
        assert conflict is not None
        self.assertIn("scheduled ON", conflict)

    def test_stop_ok_when_off(self):
        self.assertIsNone(vm.desired_conflict({"mode": "off"}, "stop", self.NOW))
        # out-of-window schedule wants OFF -> stopping agrees
        d = self.sched((dt(h=15), dt(h=18)))
        self.assertIsNone(vm.desired_conflict(d, "stop", self.NOW))

    def test_start_conflicts_when_off(self):
        conflict = vm.desired_conflict({"mode": "off"}, "start", self.NOW)
        assert conflict is not None
        self.assertIn("scheduled OFF", conflict)
        d = self.sched((dt(h=15), dt(h=18)))      # out of window -> wants off
        self.assertIsNotNone(vm.desired_conflict(d, "start", self.NOW))

    def test_start_ok_when_on(self):
        self.assertIsNone(vm.desired_conflict({"mode": "on"}, "start", self.NOW))
        d = self.sched((dt(h=10), dt(h=14)))      # in window -> wants on
        self.assertIsNone(vm.desired_conflict(d, "start", self.NOW))

    def test_restart_behaves_like_start(self):
        # restart ends running -> conflicts when schedule wants off
        self.assertIsNotNone(
            vm.desired_conflict({"mode": "off"}, "restart", self.NOW))
        self.assertIsNone(
            vm.desired_conflict({"mode": "on"}, "restart", self.NOW))


class VendorIdTests(unittest.TestCase):
    def test_live_vendor(self):
        # NVIDIA 0x10de, little-endian on the wire.
        self.assertTrue(vm._is_live_vendor(bytes([0xde, 0x10])))

    def test_inaccessible_reads_all_ones(self):
        self.assertFalse(vm._is_live_vendor(bytes([0xff, 0xff])))

    def test_zero_is_dead(self):
        self.assertFalse(vm._is_live_vendor(bytes([0x00, 0x00])))

    def test_short_read_is_dead(self):
        self.assertFalse(vm._is_live_vendor(bytes([0xde])))
        self.assertFalse(vm._is_live_vendor(b""))


class GpuPoolClassTests(unittest.TestCase):
    # gpu_pool() only counts vfio-bound display controllers; a class that
    # isn't a display function (or a non-PCI class string) must be dropped.
    def test_vga_class_counted(self):
        # Older passthrough cards (RTX 6000 class) expose 0x0300 VGA.
        self.assertTrue(vm._is_gpu_pci_class("0x030000"))

    def test_blackwell_3d_class_counted(self):
        # RTX PRO 6000 Blackwell exposes only a 3D-controller function
        # (0x0302), no VGA - the nv1 pool was empty for this reason.
        self.assertTrue(vm._is_gpu_pci_class("0x030200"))

    def test_display_other_class_counted(self):
        self.assertTrue(vm._is_gpu_pci_class("0x038000"))

    def test_audio_function_excluded(self):
        # The .1 companion function of a passthrough GPU is an audio
        # device (0x04); it must not inflate the pool.
        self.assertFalse(vm._is_gpu_pci_class("0x040300"))

    def test_empty_string_not_counted(self):
        self.assertFalse(vm._is_gpu_pci_class(""))


class SelectHealthyGpusTests(unittest.TestCase):
    POOL = ["c1:00.0", "e1:00.0", "01:00.0", "02:00.0"]

    def healthy_except(self, *wedged):
        bad = set(wedged)
        return lambda g: g not in bad

    def test_all_healthy_picks_first_n(self):
        picked, wedged = vm.select_healthy_gpus(
            self.POOL, 2, self.healthy_except())
        self.assertEqual(picked, ["c1:00.0", "e1:00.0"])
        self.assertEqual(wedged, [])

    def test_skips_wedged_in_order(self):
        # c1 wedged -> first two healthy are e1, 01.
        picked, wedged = vm.select_healthy_gpus(
            self.POOL, 2, self.healthy_except("c1:00.0"))
        self.assertEqual(picked, ["e1:00.0", "01:00.0"])
        self.assertEqual(wedged, ["c1:00.0"])

    def test_raises_when_wedging_leaves_too_few(self):
        # only one healthy, want 2 -> error names the wedged ones.
        with self.assertRaises(ValueError) as cm:
            vm.select_healthy_gpus(
                self.POOL, 2, self.healthy_except("01:00.0", "02:00.0", "e1:00.0"))
        msg = str(cm.exception)
        self.assertIn("wedged", msg)
        self.assertIn("e1:00.0", msg)

    def test_raises_plain_when_simply_not_enough(self):
        # one free, all healthy, want 2 -> no "wedged" noise.
        picked_free = ["c1:00.0"]
        with self.assertRaises(ValueError) as cm:
            vm.select_healthy_gpus(picked_free, 2, self.healthy_except())
        self.assertNotIn("wedged", str(cm.exception))

    def test_exact_fit(self):
        picked, wedged = vm.select_healthy_gpus(
            self.POOL, 4, self.healthy_except())
        self.assertEqual(len(picked), 4)


class BastionPublicPortTests(unittest.TestCase):
    """Must mirror app/templates/frps.toml.j2's port math."""

    def test_low_index_contiguous(self):
        # idx 1-3 keep historical ports: nvidia 2210+idx, compute 2213+idx.
        self.assertEqual(vm.bastion_public_port("nv1"), 2211)
        self.assertEqual(vm.bastion_public_port("nv2"), 2212)
        self.assertEqual(vm.bastion_public_port("nv3"), 2213)
        self.assertEqual(vm.bastion_public_port("c1"), 2214)
        self.assertEqual(vm.bastion_public_port("c2"), 2215)
        self.assertEqual(vm.bastion_public_port("c3"), 2216)

    def test_high_index_disjoint_blocks(self):
        # idx > 3: nvidia 2220+idx, compute 2240+idx (never collide).
        self.assertEqual(vm.bastion_public_port("nv4"), 2224)
        self.assertEqual(vm.bastion_public_port("c4"), 2244)
        self.assertEqual(vm.bastion_public_port("c5"), 2245)

    def test_unrecognised_returns_none(self):
        for bad in (None, "", "bastion", "dhcp-201", "c", "3"):
            self.assertIsNone(vm.bastion_public_port(bad))


class SshKeyValidationTests(unittest.TestCase):
    """Pure validation (no etcd) — guards the authorized_keys injection path."""

    VALID = ("ssh-ed25519 "
             "AAAAC3NzaC1lZDI1NTE5AAAAIGtestKeyMaterial0123456789abcd user@host")

    def test_accepts_any_well_formed_type(self):
        # We don't gatekeep the algorithm — any plain-token type is fine.
        for t in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                  "sk-ssh-ed25519@openssh.com", "ssh-some-future-type"):
            k = f"{t} AAAAC3NzaC1lZDI1NTE5AAAAIGtestKeyMaterial0123 me@h"
            self.assertEqual(vm.validate_ssh_key(k), k)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(vm.validate_ssh_key(f"  {self.VALID}  \n"), self.VALID)

    def test_rejects_embedded_newline(self):
        # The authorized_keys injection vector: a second line would be an
        # entry WITHOUT the restrict/port-forwarding options.
        with self.assertRaises(ValueError):
            vm.validate_ssh_key(self.VALID + "\nssh-rsa AAAAB3NzaInjected attacker")

    def test_rejects_tab_and_controls(self):
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-ed25519\tAAAAC3NzaC1lZDI1NTE5AAAAIGtest0123 x")

    def test_rejects_type_with_special_chars(self):
        # Shape check still rejects shell-ish junk in the type token.
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-rsa;evil AAAAC3NzaC1lZDI1NTE5AAAAIGtest0123 x")

    def test_rejects_non_base64_material(self):
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-ed25519 not-valid-base64!! x")

    def test_rejects_short_material(self):
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-ed25519 AAAA x")

    def test_rejects_missing_material(self):
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-ed25519")

    def test_rejects_empty(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                vm.validate_ssh_key(bad)

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            vm.validate_ssh_key("ssh-ed25519 " + "A" * 5000 + " x")


class VmAccessTests(unittest.TestCase):
    """Registry -> SSH access mapping: owner + additional users."""

    KEY_A = ("ssh-ed25519 "
             "AAAAC3NzaC1lZDI1NTE5AAAAIKKEYMATERIAL00000AAAa alice@x")
    KEY_B = ("ssh-ed25519 "
             "AAAAC3NzaC1lZDI1NTE5AAAAIKKEYMATERIAL11111BBb bob@x")
    KEY_SHARED = ("ssh-ed25519 "
                  "AAAAC3NzaC1lZDI1NTE5AAAAIKKEYMATERIAL99999ZZz shared@x")

    def _rec(self, owner, additional=()):
        return {"owner": owner, "additional_users": list(additional),
                "gpus": 1, "host": "nv2", "type": "vm"}

    def test_access_users_owner_plus_additional(self):
        self.assertEqual(
            vm._vm_access_users(self._rec("a@x", ["b@x", "c@x"])),
            ["a@x", "b@x", "c@x"])

    def test_access_users_dedupes_and_skips_blank(self):
        self.assertEqual(
            vm._vm_access_users(self._rec("a@x", ["a@x", "b@x", ""])),
            ["a@x", "b@x"])

    def test_access_users_empty(self):
        self.assertEqual(vm._vm_access_users({}), [])
        self.assertEqual(vm._vm_access_users({"owner": None}), [])

    @mock.patch.object(vm, "vms_all")
    def test_access_vms_maps_owner_and_additional(self, vms_all):
        vms_all.return_value = {
            "vm1": self._rec("a@x", ["b@x"]),
            "vm2": self._rec("b@x"),
        }
        self.assertEqual(
            vm._access_vms(),
            {"a@x": ["vm1"], "b@x": ["vm1", "vm2"]})

    @mock.patch.object(vm, "user_get")
    def test_access_keys_union_deduped(self, user_get):
        user_get.side_effect = lambda u: (
            {"ssh_keys": [self.KEY_A]} if u == "a@x"
            else {"ssh_keys": [self.KEY_B]})
        rec = self._rec("a@x", ["a@x", "b@x"])
        self.assertEqual(vm._vm_access_keys(rec), [self.KEY_A, self.KEY_B])

    @mock.patch.object(vm, "users_all")
    @mock.patch.object(vm, "vms_all")
    def test_bastion_permitopen_for_owner_and_additional(self, vms_all,
                                                         users_all):
        vms_all.return_value = {"vm1": self._rec("a@x", ["b@x"])}
        users_all.return_value = {
            "a@x": {"ssh_keys": [self.KEY_A]},
            "b@x": {"ssh_keys": [self.KEY_B]},
        }
        out = vm._bastion_authorized_keys()
        self.assertIn(
            f'restrict,port-forwarding,permitopen="vm1:22" {self.KEY_A}', out)
        self.assertIn(
            f'restrict,port-forwarding,permitopen="vm1:22" {self.KEY_B}', out)

    @mock.patch.object(vm, "users_all")
    @mock.patch.object(vm, "vms_all")
    def test_bastion_dedupes_shared_key_unions_permitopen(self, vms_all,
                                                          users_all):
        vms_all.return_value = {
            "vm1": self._rec("a@x"),
            "vm2": self._rec("b@x"),
        }
        users_all.return_value = {
            "a@x": {"ssh_keys": [self.KEY_SHARED]},
            "b@x": {"ssh_keys": [self.KEY_SHARED]},
        }
        out = vm._bastion_authorized_keys()
        lines = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertIn('permitopen="vm1:22"', lines[0])
        self.assertIn('permitopen="vm2:22"', lines[0])

    @mock.patch.object(vm, "user_get")
    @mock.patch.object(vm, "_instance_exists")
    @mock.patch.object(vm, "vm_get")
    def test_alter_add_user_without_keys_raises(self, vm_get, inst_exists,
                                                user_get):
        vm_get.return_value = self._rec("a@x")
        inst_exists.return_value = True
        user_get.return_value = None          # added user has no record
        with self.assertRaises(ValueError):
            vm.vm_alter("vm1", add_users=["b@x"])


def _proc(returncode, stderr=""):
    return subprocess.CompletedProcess(["incus"], returncode, stderr=stderr)


class IncusStopTests(unittest.TestCase):
    """_incus_stop bounds the stop and only force-stops containers."""

    def _stop(self, type_, *incus_effects):
        with mock.patch.object(vm, "_instance_info",
                               return_value={"type": type_}), \
             mock.patch.object(vm, "_incus", side_effect=incus_effects) as m:
            result = vm._incus_stop("x")
        return result, m

    def test_container_graceful_stop_succeeds_no_force(self):
        result, m = self._stop("container", _proc(0))
        self.assertEqual(result.returncode, 0)
        m.assert_called_once_with("stop", "x",
                                  timeout=vm._STOP_TIMEOUT_CONTAINER_S)

    def test_vm_graceful_stop_succeeds_no_force(self):
        result, m = self._stop("virtual-machine", _proc(0))
        self.assertEqual(result.returncode, 0)
        m.assert_called_once_with("stop", "x",
                                  timeout=vm._STOP_TIMEOUT_VM_S)

    def test_container_falls_back_to_force_on_timeout(self):
        result, m = self._stop("container",
                               subprocess.TimeoutExpired("incus stop", 90),
                               _proc(0))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(m.call_count, 2)
        # Second call adds --force, bounded by the force window.
        self.assertEqual(m.call_args_list[1].args, ("stop", "x", "--force"))
        self.assertEqual(
            m.call_args_list[1].kwargs["timeout"], vm._STOP_TIMEOUT_FORCE_S)

    def test_container_force_failure_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._stop("container",
                       subprocess.TimeoutExpired("incus stop", 90),
                       _proc(1, "boom"))
        self.assertIn("boom", str(ctx.exception))

    def test_vm_timeout_never_forces(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._stop("virtual-machine",
                       subprocess.TimeoutExpired("incus stop", 180))
        self.assertIn("NOT forcing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class VMAlterHelperTests(unittest.TestCase):
    def _rec(self, **kw):
        base = {"name": "vm1", "owner": "a@x", "type": "vm", "gpus": 1}
        base.update(kw)
        return base

    def test_change_summary(self):
        rec = {"additional_users": ["b@x"]}
        changes = vm_alter._vm_alter_changes(
            "new@x", True, rec, True, 2, True, 8, True, "16GiB", True)
        self.assertEqual(changes, [
            "owner -> new@x", "additional users -> [b@x]",
            "gpus -> 2", "cpu -> 8", "mem -> 16GiB",
        ])

    @mock.patch.object(vm, "_verify_guest_gpus")
    @mock.patch.object(vm, "_wait_agent")
    @mock.patch.object(vm, "_incus")
    @mock.patch.object(vm, "_ensure_nas_share")
    @mock.patch.object(vm, "_ensure_gpus_attached")
    @mock.patch.object(vm, "_put_json")
    @mock.patch.object(vm, "release_gpus")
    @mock.patch.object(vm, "_incus_stop")
    @mock.patch.object(vm, "_instance_info")
    def test_vm_gpu_change_running(self, info, stop, release, put, attach,
                                   nas, incus, wait, verify):
        rec = self._rec()
        info.return_value = {"status": "Running"}
        self.assertTrue(vm_alter._alter_vm_gpus("vm1", rec, "vm", 2))
        stop.assert_called_once_with("vm1")
        release.assert_called_once_with("vm1")
        put.assert_called_once()
        attach.assert_called_once_with("vm1")
        nas.assert_called_once_with("vm1")
        incus.assert_any_call("start", "vm1")
        verify.assert_called_once_with("vm1", 2)
        self.assertEqual(rec["gpus"], 2)

    @mock.patch.object(vm, "_instance_info",
                       return_value={"status": "Stopped"})
    @mock.patch.object(vm, "_incus_stop")
    @mock.patch.object(vm, "release_gpus")
    @mock.patch.object(vm, "_put_json")
    @mock.patch.object(vm, "_ensure_gpus_attached")
    def test_vm_gpu_change_stopped_no_gpu(self, attach, put, release, stop,
                                          info):
        rec = self._rec()
        self.assertTrue(vm_alter._alter_vm_gpus("vm1", rec, "vm", 0))
        stop.assert_not_called()
        release.assert_called_once_with("vm1")
        attach.assert_not_called()
        self.assertEqual(rec["gpus"], 0)

    @mock.patch.object(vm, "_incus")
    @mock.patch.object(vm, "_instance_info", return_value={"devices": {}})
    def test_container_gpu_add_and_remove(self, info, incus):
        rec = self._rec(type="container", gpus=0)
        self.assertTrue(vm_alter._alter_vm_gpus("vm1", rec, "container", 1))
        incus.assert_called_once_with("config", "device", "add", "vm1",
                                      "gpu0", "gpu", "gputype=physical")

        rec = self._rec(type="container", gpus=1)
        self.assertTrue(vm_alter._alter_vm_gpus("vm1", rec, "container", 0))
        incus.assert_called_with("config", "device", "remove", "vm1",
                                 "gpu0", check=False)
        self.assertEqual(rec["gpus"], 0)

    @mock.patch.object(vm, "_incus")
    @mock.patch.object(vm, "_instance_info",
                       return_value={"devices": {"gpu0": {"type": "gpu"}}})
    def test_container_gpu_already_present(self, info, incus):
        rec = self._rec(type="container", gpus=0)
        self.assertTrue(vm_alter._alter_vm_gpus("vm1", rec, "container", 1))
        incus.assert_not_called()
        self.assertEqual(rec["gpus"], 1)


class VMAlterOrchestratorTests(unittest.TestCase):
    def _patches(self):
        return [
            mock.patch.object(vm, "_valid_vm_name"),
            mock.patch.object(vm, "_instance_exists", return_value=True),
            mock.patch.object(vm, "_put_json"),
            mock.patch.object(vm, "_record_event"),
        ]

    @mock.patch.object(vm, "vm_get")
    def test_vm_alter_no_changes(self, vm_get):
        rec = {"name": "vm1", "owner": "a@x", "type": "vm", "gpus": 1,
               "cpu": "4", "mem": "8GiB", "additional_users": ["b@x"]}
        vm_get.return_value = rec
        with contextlib.ExitStack() as stack:
            put = stack.enter_context(mock.patch.object(vm, "_put_json"))
            event = stack.enter_context(mock.patch.object(vm, "_record_event"))
            stack.enter_context(mock.patch.object(vm, "_instance_exists", return_value=True))
            stack.enter_context(mock.patch.object(vm, "user_get", return_value={"ssh_keys": ["key"]}))
            vm.vm_alter("vm1", owner="a@x", gpus=1, cpu="4", mem="8GiB",
                        add_users=["b@x"], remove_users=[])
        self.assertEqual(put.call_count, 0)
        self.assertEqual(event.call_count, 0)

    @mock.patch.object(vm, "bastion_sync")
    @mock.patch.object(vm, "_sync_vm_keys")
    @mock.patch.object(vm, "_record_event")
    @mock.patch.object(vm, "_put_json")
    @mock.patch.object(vm, "_incus")
    @mock.patch.object(vm, "_valid_user")
    @mock.patch.object(vm, "user_get")
    @mock.patch.object(vm, "_instance_info")
    @mock.patch.object(vm, "vm_get")
    @mock.patch.object(vm, "_valid_vm_name")
    def test_vm_alter_applies_changes(self, valid_name, vm_get, info,
                                      user_get, valid_user, incus, put, event,
                                      sync_keys, bastion_sync):
        rec = {"name": "vm1", "owner": "a@x", "type": "container", "gpus": 0,
               "cpu": "4", "mem": "8GiB"}
        vm_get.return_value = rec
        info.return_value = {"status": "Stopped", "devices": {"gpu0": {"type": "gpu"}}}
        user_get.return_value = {"ssh_keys": ["key"]}
        with contextlib.ExitStack() as stack:
            _ = stack.enter_context(mock.patch.object(vm, "_instance_exists", return_value=True))
            vm.vm_alter(
                "vm1", owner="new@x", gpus=1, cpu="8", mem="16GiB",
                add_users=["b@x"], remove_users=[])
        put.assert_called_once()
        sync_keys.assert_called_once()
        bastion_sync.assert_called_once()
        event.assert_called_once()
        self.assertEqual(rec["owner"], "new@x")
        self.assertEqual(rec["additional_users"], ["b@x"])
        self.assertEqual(rec["gpus"], 1)
        self.assertEqual(rec["cpu"], "8")
        self.assertEqual(rec["mem"], "16GiB")

    @mock.patch.object(vm, "_instance_exists", return_value=False)
    @mock.patch.object(vm, "vm_get", return_value={"host": "elsewhere"})
    @mock.patch.object(vm, "_valid_vm_name")
    def test_vm_alter_requires_local_instance(self, valid_name, vm_get,
                                              exists):
        with self.assertRaisesRegex(ValueError, "not present on this host"):
            vm_alter.vm_alter("vm1")

    @mock.patch.object(vm, "_instance_exists", return_value=True)
    @mock.patch.object(vm, "vm_get", return_value={"type": "vm"})
    @mock.patch.object(vm, "_valid_vm_name")
    def test_vm_alter_rejects_image_type_negative_gpu(self, valid_name,
                                                      vm_get, exists):
        with self.assertRaises(NotImplementedError):
            vm_alter.vm_alter("vm1", image="new")
        with self.assertRaises(NotImplementedError):
            vm_alter.vm_alter("vm1", instance_type="container")
        with self.assertRaises(ValueError):
            vm_alter.vm_alter("vm1", gpus=-1)


class ReconcileStopTests(unittest.TestCase):
    def _test_stop(self, grace, now, mock_stop, issues=None):
        issues = issues if issues is not None else {}
        with mock.patch.object(vm, 'vm_stop') as _mock_stop, \
             mock.patch.object(vm, 'grace_delete'), \
             mock.patch.object(vm, 'release_gpus', return_value=2), \
             mock.patch.object(vm, '_clear_issue') as clear:
            _mock_stop.return_value = None
            vm._reconcile_stop('vm1', grace, now, issues)
        return _mock_stop, clear

    @mock.patch.object(vm, '_clear_issue')
    @mock.patch.object(vm, 'vm_stop')
    @mock.patch.object(vm, 'grace_delete')
    @mock.patch.object(vm, 'release_gpus', return_value=2)
    def test_immediate_stop(self, release, gd, stop, clear):
        now = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
        grace = {'warned_at': now.isoformat(), 'immediate': True}
        issues = {'vm1': True}
        vm._reconcile_stop('vm1', grace, now, issues)
        stop.assert_called_once_with('vm1', initiator='scheduler')
        clear.assert_called_once_with(issues, 'vm1')

    @mock.patch.object(vm, 'vm_stop')
    @mock.patch.object(vm, 'grace_delete')
    @mock.patch.object(vm, 'release_gpus', return_value=0)
    def test_grace_elapsed_stop(self, release, gd, stop):
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        warned = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
        grace = {'warned_at': warned.isoformat()}
        vm._reconcile_stop('vm1', grace, now, {})
        stop.assert_called_once_with('vm1', initiator='scheduler')

    @mock.patch.object(vm, 'vm_stop', side_effect=RuntimeError('stop failed'))
    def test_stop_failure_records_issue(self, stop):
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        grace = {'immediate': True}
        issues = {}
        with mock.patch.object(vm, '_record_issue') as record:
            vm._reconcile_stop('vm1', grace, now, issues)
        record.assert_called_once()

    @mock.patch.object(vm, 'vm_stop')
    @mock.patch.object(vm, 'grace_delete')
    @mock.patch.object(vm, 'release_gpus', return_value=0)
    def test_no_warned_at_stops(self, release, gd, stop):
        now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        grace = {}
        vm._reconcile_stop('vm1', grace, now, {})
        stop.assert_called_once_with('vm1', initiator='scheduler')


class ReconcileWantOnTests(unittest.TestCase):
    def test_running_clears_issue(self):
        issues = {'vm1': True}
        with mock.patch.object(vm, '_clear_issue') as clear:
            vm._reconcile_want_on('vm1', {'mode': 'on'}, True, None,
                                  issues, [], {'vm1': {'gpus': 1}})
        clear.assert_called_once_with(issues, 'vm1')

    @mock.patch.object(vm, 'grace_delete')
    @mock.patch.object(vm, 'desired_gpus', return_value=2)
    def test_not_running_appends_to_start(self, dg, gd):
        to_start = []
        all_vms = {'vm1': {'gpus': 1}}
        with mock.patch.object(vm, '_clear_issue'):
            vm._reconcile_want_on('vm1', {'mode': 'on'}, False, None,
                                  {}, to_start, all_vms)
        self.assertEqual(to_start, [('vm1', 2)])
        gd.assert_not_called()

    @mock.patch.object(vm, 'grace_delete')
    @mock.patch.object(vm, 'desired_gpus', return_value=1)
    def test_grace_deleted_on_flip_back(self, dg, gd):
        to_start = []
        with mock.patch.object(vm, '_clear_issue'):
            vm._reconcile_want_on('vm1', {'mode': 'on'}, True,
                                  {'warned_at': 'x'}, {}, to_start,
                                  {'vm1': {'gpus': 1}})
        gd.assert_called_once_with('vm1')
        self.assertEqual(to_start, [])


class ValidateVMLaunchTests(unittest.TestCase):
    @mock.patch.object(vm, '_instance_exists', return_value=False)
    @mock.patch.object(vm, 'vm_get', return_value=None)
    @mock.patch.object(vm, 'user_get')
    @mock.patch.object(vm, '_valid_vm_name')
    def test_valid_launch(self, valid_name, user_get, vm_get, exists):
        user_get.return_value = {'ssh_keys': ['key']}
        result = vm._validate_vm_launch('vm1', 'a@x', 1, 'vm')
        self.assertEqual(result, 'vm')

    @mock.patch.object(vm, 'user_get', return_value=None)
    @mock.patch.object(vm, '_valid_vm_name')
    def test_no_keys_raises(self, valid_name, user_get):
        with self.assertRaisesRegex(ValueError, 'no SSH keys'):
            vm._validate_vm_launch('vm1', 'a@x', 1, 'vm')

    @mock.patch.object(vm, '_valid_vm_name')
    def test_negative_gpus_raises(self, valid_name):
        with self.assertRaisesRegex(ValueError, 'cannot be negative'):
            vm._validate_vm_launch('vm1', 'a@x', -1, 'vm')

    @mock.patch.object(vm, 'user_get', return_value={'ssh_keys': ['k']})
    @mock.patch.object(vm, '_valid_vm_name')
    def test_bad_type_raises(self, valid_name, user_get):
        with self.assertRaisesRegex(ValueError, 'Invalid --type'):
            vm._validate_vm_launch('vm1', 'a@x', 1, 'lxc')

    @mock.patch.object(vm, 'user_get', return_value={'ssh_keys': ['k']})
    @mock.patch.object(vm, '_instance_exists', return_value=True)
    @mock.patch.object(vm, 'vm_get', return_value=None)
    @mock.patch.object(vm, '_valid_vm_name')
    def test_already_exists_raises(self, valid_name, vm_get, exists, user_get):
        with self.assertRaisesRegex(ValueError, 'already exists'):
            vm._validate_vm_launch('vm1', 'a@x', 1, 'vm')


class GpuPoolDiscoveryTests(unittest.TestCase):
    def test_gpu_entry_uses_pci_class(self):
        with mock.patch.object(vm, '_device_pci_class', return_value='0x030200'):
            self.assertTrue(vm._is_gpu_pci_entry('c1:00.0'))
        with mock.patch.object(vm, '_device_pci_class', return_value='0x040300'):
            self.assertFalse(vm._is_gpu_pci_entry('c1:00.1'))

    def test_read_error_excluded(self):
        with mock.patch.object(vm, '_device_pci_class', return_value=None):
            self.assertFalse(vm._is_gpu_pci_entry('x'))
