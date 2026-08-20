"""Web-app tests for the admin VM-schedule endpoints.

Drives the Flask app through its test client with a dict-backed fake etcd
patched over the DAL seam (ycluster.utils.vm_manager.get_etcd_client).
No etcd/postgres server, no DB drivers — psycopg2 is a lazy import the
schedule endpoints don't touch, and the etcd client is faked.

Both vm_manager and app.py do `from ... import get_etcd_client`, which
binds the name separately in each module's namespace. Patching just one
isn't enough — routes in app.py that call `get_etcd_client()` need the
patched version too, so setUp rebinds both.

Run from config/ansible/admin/files:
    PYTHONPATH=.:ycluster python3 -m unittest discover -s tests
(or via ./run-tests.sh at the repo root, which also runs the package tests).
"""

import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))      # .../admin/files/tests
_FILES = os.path.dirname(_HERE)                          # .../admin/files
for p in (_HERE, _FILES, os.path.join(_FILES, "ycluster")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_etcd import FakeEtcd                           # noqa: E402
import app as appmod                                     # noqa: E402
from ycluster.common import apikeys                      # noqa: E402
from ycluster.utils import vm_manager as vmm             # noqa: E402

UTC = timezone.utc


def _fast_hasher():
    """A PasswordHasher subclass with low cost — still argon2id, per-key
    salt, just fast enough for a handful of creates in a test run."""
    from argon2 import PasswordHasher as _PH
    return type("FastPH", (_PH,), {})(time_cost=1, memory_cost=1024,
                                      hash_len=16, salt_len=8)


def _iso(dt):
    return dt.isoformat()


class SchedTestBase(unittest.TestCase):
    def setUp(self):
        self.etcd = FakeEtcd()
        # Both modules import get_etcd_client at module load — they each
        # have their own name binding. Patch all so routes in app.py
        # that call get_etcd_client() hit the FakeEtcd instead of trying
        # to connect to localhost:2379. The apikeys DAL too.
        self._vmm_orig = vmm.get_etcd_client
        self._app_orig = appmod.get_etcd_client
        self._ak_orig = apikeys.get_etcd_client
        self._ph_orig = apikeys.PasswordHasher
        vmm.get_etcd_client = lambda: self.etcd
        appmod.get_etcd_client = lambda: self.etcd
        apikeys.get_etcd_client = lambda: self.etcd
        apikeys.PasswordHasher = _fast_hasher
        self.http = appmod.app.test_client()
        # Manager-scoped endpoints (VM schedule, resources) consult
        # authentik_manager.manager_projects / user_project. Default a test
        # manager to a catch-all project so the legacy "manager sees the
        # full in-scope roster" tests keep their meaning; the project-scoping
        # tests override these with explicit stubs. Never touches a real IdP.
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["__any__"]
        am.user_project = lambda email: "__any__"

    def tearDown(self):
        vmm.get_etcd_client = self._vmm_orig
        appmod.get_etcd_client = self._app_orig
        apikeys.get_etcd_client = self._ak_orig
        apikeys.PasswordHasher = self._ph_orig

    # --- seeding ---
    def vm(self, name, owner="alice@x", gpus=0, host="nv2", type="vm",
           cpu=None, mem=None):
        rec = {"owner": owner, "gpus": gpus, "host": host, "type": type}
        if cpu is not None:
            rec["cpu"] = cpu
        if mem is not None:
            rec["mem"] = mem
        self.etcd.seed_json(vmm.VMS_PREFIX + name, rec)

    def desired(self, name, mode, windows=None, gpus=None):
        rec = {"mode": mode, "windows": windows or []}
        if gpus is not None:
            rec["gpus"] = gpus
        self.etcd.seed_json(vmm.VM_DESIRED_PREFIX + name, rec)

    def state(self, host, pool=None, instances=None):
        snap = {"instances": instances or []}
        if pool is not None:
            snap["gpu_pool"] = pool
        self.etcd.seed_json(vmm.VM_STATE_PREFIX + host, snap)

    # --- requests (admin = internal, no forward-auth headers; manager =
    # authenticated user whose X-Authentik-Groups carries ycluster-managers
    # but not ycluster-admins) ---
    # UID is the primary identity signal in _authentik_identity — set
    # for every authenticated user (including OAuth-sourced accounts
    # without a verified email). Tests that simulate an OAuth-no-email
    # user pass uid=True without email; everything else gets both.
    def _headers(self, admin, email, manager=False, uid=True):
        if admin:
            return {}                                            # operator space
        h = {"X-Authentik-Groups": (
            "ycluster-managers" if manager else "users")}
        if uid:
            h["X-Authentik-UID"] = "uid-" + (email or "anon")
        if email:
            h["X-Authentik-Email"] = email
        return h

    # Flask's test client sets REMOTE_ADDR via environ_overrides (not via
    # the headers dict, which becomes HTTP_* entries). Default is 127.0.0.1
    # — explicit `remote_addr` lets belt-and-suspenders tests simulate a
    # non-loopback caller.
    def _environ(self, remote_addr):
        return {"REMOTE_ADDR": remote_addr} if remote_addr else {}

    def post(self, path, body, admin=False, email="alice@x", manager=False,
             remote_addr="127.0.0.1"):
        return self.http.post(path, json=body,
                              headers=self._headers(admin, email, manager),
                              environ_overrides=self._environ(remote_addr))

    def get(self, path, admin=False, email="alice@x", manager=False,
            remote_addr="127.0.0.1"):
        return self.http.get(path,
                             headers=self._headers(admin, email, manager),
                             environ_overrides=self._environ(remote_addr))


class ScheduleSetTests(SchedTestBase):
    def test_no_such_vm(self):
        self.assertEqual(self.post("/admin/vm-schedule/set",
                                   {"vm": "ghost", "mode": "off"}).status_code, 404)

    def test_invalid_name_is_404(self):
        self.assertEqual(self.post("/admin/vm-schedule/set",
                                   {"vm": "../etc", "mode": "off"}).status_code, 404)

    def test_bad_mode(self):
        self.vm("v")
        self.assertEqual(self.post("/admin/vm-schedule/set",
                                   {"vm": "v", "mode": "bogus"}).status_code, 400)

    def test_not_your_vm(self):
        self.vm("v", owner="bob@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off"},
                      email="alice@x")
        self.assertEqual(r.status_code, 403)

    def test_owner_schedules_no_record_vm(self):
        # No desired record yet (implicit unmanaged) — owner may schedule it.
        self.vm("v", owner="alice@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off"})
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        assert rec is not None
        self.assertEqual(rec["mode"], "off")

    def test_nonadmin_cannot_set_unmanaged(self):
        self.vm("v", owner="alice@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "unmanaged"})
        self.assertEqual(r.status_code, 403)

    def test_nonadmin_cannot_change_admin_set_unmanaged(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "unmanaged")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "on"})
        self.assertEqual(r.status_code, 403)

    def test_admin_can_set_unmanaged(self):
        self.vm("v", owner="alice@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "unmanaged"},
                      admin=True)
        self.assertEqual(r.status_code, 200)

    def test_admin_can_change_unmanaged(self):
        self.vm("v")
        self.desired("v", "unmanaged")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off"},
                      admin=True)
        self.assertEqual(r.status_code, 200)

    def test_windows_preserved_across_off(self):
        self.vm("v", owner="alice@x")
        # Future dates so the windows aren't filtered as "fully elapsed" by
        # _parse_windows (which drops windows whose end <= now).
        base = datetime.now(UTC) + timedelta(days=2)
        s = base.replace(hour=10, minute=0, second=0, microsecond=0)
        e = s + timedelta(hours=4)
        w = [{"start": _iso(s), "end": _iso(e)}]
        self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "schedule", "windows": w})
        self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off", "windows": w})
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        assert rec is not None
        self.assertEqual(rec["mode"], "off")
        self.assertEqual(len(rec["windows"]), 1)         # preserved, not cleared

    def test_owner_off_to_on(self):
        # Regression: a VM that's already in 'off' state must be editable
        # by its owner (POST /admin/vm-schedule/set returns 200, not 403).
        # The original UI bug was a disabled dropdown; this is the API
        # counterpart — both must agree.
        self.vm("v", owner="alice@x")
        self.desired("v", "off")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "on"},
                      email="alice@x")
        self.assertEqual(r.status_code, 200,
                         "owner must be able to flip off->on via Save")
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        assert rec is not None
        self.assertEqual(rec["mode"], "on")

    def test_owner_off_to_schedule(self):
        # Same regression: off -> schedule (with windows) must succeed.
        self.vm("v", owner="alice@x")
        self.desired("v", "off")
        base = datetime.now(UTC) + timedelta(days=2)
        s = base.replace(hour=10, minute=0, second=0, microsecond=0)
        e = s + timedelta(hours=4)
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "schedule",
                       "windows": [{"start": _iso(s), "end": _iso(e)}]},
                      email="alice@x")
        self.assertEqual(r.status_code, 200,
                         "owner must be able to flip off->schedule via Save")
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        assert rec is not None
        self.assertEqual(rec["mode"], "schedule")

    def test_owner_on_to_off(self):
        # The reverse transition (on -> off) — same regression coverage.
        self.vm("v", owner="alice@x")
        self.desired("v", "on")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off"},
                      email="alice@x")
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        assert rec is not None
        self.assertEqual(rec["mode"], "off")

    def test_can_edit_matches_api_save_succeeds(self):
        # Belt-and-suspenders: when the UI says the dropdown is enabled
        # (can_edit=True on a row), the corresponding POST must also
        # succeed — otherwise the page lies to the user.
        self.vm("v", owner="alice@x")
        self.desired("v", "off")
        rows = self.get("/admin/vm-schedule/data",
                        email="alice@x").get_json()["rows"]
        self.assertTrue(rows[0]["can_edit"])
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "on"},
                      email="alice@x")
        self.assertEqual(r.status_code, 200,
                         "can_edit=True must imply POST /set returns 200 "
                         "for a mode that doesn't require admin")

    def test_schedule_invalid_windows_rejected(self):
        self.vm("v", owner="alice@x")
        bad = [{"start": "not-a-date", "end": "nope"}]
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "schedule", "windows": bad})
        self.assertEqual(r.status_code, 400)

    def test_admission_fails_open_without_pool(self):
        # GPU VM, mode on, but the host never reported a pool -> accept.
        self.vm("v", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "on"})
        self.assertEqual(r.status_code, 200)

    def test_admission_rejects_overcommit(self):
        # nv2 pool = 2; 'other' holds 2 always (mode on); 'mine' wants 2 -> 409.
        self.state("nv2", pool=2)
        self.vm("other", owner="bob@x", gpus=2, host="nv2")
        self.desired("other", "on")
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set", {"vm": "mine", "mode": "on"},
                      admin=True)
        self.assertEqual(r.status_code, 409)


    def test_default_gpus_is_registered_count(self):
        # No `gpus` in the request (old clients) => all allocated GPUs.
        self.vm("v", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "on"})
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        self.assertEqual(rec["gpus"], 2)

    def test_explicit_gpus_stored(self):
        self.vm("v", owner="alice@x", gpus=4, host="nv2")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "on", "gpus": 1})
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        self.assertEqual(rec["gpus"], 1)

    def test_gpus_zero_allowed_on_gpu_vm(self):
        # Scheduling 0 GPUs runs the VM without holding any (CPU-only run).
        self.vm("v", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "on", "gpus": 0})
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        self.assertEqual(rec["gpus"], 0)

    def test_gpus_over_registered_rejected(self):
        self.vm("v", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "on", "gpus": 3})
        self.assertEqual(r.status_code, 400)

    def test_cpu_only_vm_requires_zero_gpus(self):
        self.vm("v", owner="alice@x", gpus=0, host="nv2")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "on", "gpus": 1})
        self.assertEqual(r.status_code, 400)
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "v", "mode": "on", "gpus": 0})
        self.assertEqual(r.status_code, 200)

    def test_admission_uses_chosen_gpus(self):
        # nv2 pool = 3; 'other' holds 2 always; 'mine' is registered for 2.
        # Choosing 2 over-commits (1 free) -> 409; choosing 1 fits -> 200.
        self.state("nv2", pool=3)
        self.vm("other", owner="bob@x", gpus=2, host="nv2")
        self.desired("other", "on")
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set", {"vm": "mine", "mode": "on",
                                                 "gpus": 2}, admin=True)
        self.assertEqual(r.status_code, 409)
        r = self.post("/admin/vm-schedule/set", {"vm": "mine", "mode": "on",
                                                 "gpus": 1}, admin=True)
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "mine")
        self.assertEqual(rec["gpus"], 1)


class StopNowTests(SchedTestBase):
    def test_pending_stop_stamps_immediate(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "off")                         # wants off -> pending stop
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "v"})
        self.assertEqual(r.status_code, 200)
        g = self.etcd.get_json(vmm.VM_GRACE_PREFIX + "v")
        assert g is not None
        self.assertTrue(g["immediate"])

    def test_no_pending_stop_when_wants_on(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "on")                          # wants on -> nothing to stop
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "v"})
        self.assertEqual(r.status_code, 409)

    def test_not_your_vm(self):
        self.vm("v", owner="bob@x")
        self.desired("v", "off")
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "v"}, email="alice@x")
        self.assertEqual(r.status_code, 403)


class GpuBlockApiTests(SchedTestBase):
    """Admin GPU reservations: admin-only create/edit/delete, admission
    accounting against VM commitments, and data exposure on the schedule
    page."""

    def _win(self, hours_from_now=48):
        base = (datetime.now(UTC) + timedelta(hours=hours_from_now)) \
            .replace(minute=0, second=0, microsecond=0)
        s = base.replace(hour=10)
        e = s + timedelta(hours=4)
        return [{"start": _iso(s), "end": _iso(e)}]

    def test_non_admin_and_manager_denied(self):
        for kw in ({}, {"manager": True}):
            r = self.post("/admin/vm-schedule/gpu-block",
                          {"host": "nv2", "gpus": 2, "mode": "on"}, **kw)
            self.assertEqual(r.status_code, 403)
        r = self.post("/admin/vm-schedule/gpu-block/delete",
                      {"block": "block-x"})
        self.assertEqual(r.status_code, 403)

    def test_admin_creates_on_block(self):
        self.state("nv2", pool=4)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on",
                       "reason": "maintenance"}, admin=True)
        self.assertEqual(r.status_code, 200)
        block_id = r.get_json()["block"]
        self.assertTrue(block_id.startswith("block-"))
        rec = self.etcd.get_json(vmm.GPU_BLOCKS_PREFIX + block_id)
        assert rec is not None
        self.assertEqual(rec["host"], "nv2")
        self.assertEqual(rec["gpus"], 2)
        self.assertEqual(rec["reason"], "maintenance")

    def test_block_counts_against_vm_admission(self):
        # pool 2; an 'on' block of 2 -> a 2-GPU VM no longer fits. The block
        # is created first (nothing committed yet); unmanaged VMs are
        # committed *always*, so a pre-seeded unmanaged VM would itself have
        # blocked the reservation. The VM is registered afterwards.
        self.state("nv2", pool=2)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on"}, admin=True)
        self.assertEqual(r.status_code, 200)
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        r = self.post("/admin/vm-schedule/set", {"vm": "mine", "mode": "on"})
        self.assertEqual(r.status_code, 409)

    def test_block_rejected_by_vm_commitment(self):
        # pool 2; 'other' holds 2 always -> an 'on' block of 2 is overcommit.
        self.state("nv2", pool=2)
        self.vm("other", owner="bob@x", gpus=2, host="nv2")
        self.desired("other", "on")
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on"}, admin=True)
        self.assertEqual(r.status_code, 409)

    def test_block_fails_open_without_pool(self):
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on"}, admin=True)
        self.assertEqual(r.status_code, 200)

    def test_schedule_block_exposed_to_admin_and_manager(self):
        self.state("nv2", pool=4)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 1, "mode": "schedule",
                       "windows": self._win()}, admin=True)
        self.assertEqual(r.status_code, 200)
        block_id = r.get_json()["block"]

        for kw in ({"admin": True}, {"manager": True}):
            d = self.get("/admin/vm-schedule/data", **kw).get_json()
            self.assertEqual([b["block"] for b in d["blocks"]], [block_id])
            nv2 = next(h for h in d["hosts"] if h["host"] == "nv2")
            self.assertTrue(any(c["is_block"] for c in nv2["commitments"]))

        # A plain user doesn't see the block roster (only its capacity
        # effect in commitments), and hosts they don't touch stay hidden.
        self.vm("a", owner="alice@x", host="nv2")
        d = self.get("/admin/vm-schedule/data").get_json()
        self.assertEqual(d["blocks"], [])

    def test_edit_existing_block(self):
        self.state("nv2", pool=4)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on", "reason": "r1"},
                      admin=True)
        block_id = r.get_json()["block"]
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"block": block_id, "host": "nv2", "gpus": 3,
                       "mode": "on", "reason": "r2"}, admin=True)
        self.assertEqual(r.status_code, 200)
        rec = self.etcd.get_json(vmm.GPU_BLOCKS_PREFIX + block_id)
        self.assertEqual(rec["gpus"], 3)
        self.assertEqual(rec["reason"], "r2")
        self.assertEqual(rec["created_by"], "internal")  # admin = operator space

    def test_invalid_inputs(self):
        self.state("nv2", pool=4)
        for bad in (0, -1, "x", 1.5):
            r = self.post("/admin/vm-schedule/gpu-block",
                          {"host": "nv2", "gpus": bad, "mode": "on"}, admin=True)
            self.assertEqual(r.status_code, 400, f"gpus={bad!r}")
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "schedule"},
                      admin=True)
        self.assertEqual(r.status_code, 400)     # schedule without windows
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "BAD HOST", "gpus": 2, "mode": "on"}, admin=True)
        self.assertEqual(r.status_code, 400)

    def test_delete_block(self):
        self.state("nv2", pool=4)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on"}, admin=True)
        block_id = r.get_json()["block"]
        r = self.post("/admin/vm-schedule/gpu-block/delete",
                      {"block": block_id}, admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.etcd.get_json(vmm.GPU_BLOCKS_PREFIX + block_id))
        r = self.post("/admin/vm-schedule/gpu-block/delete",
                      {"block": block_id}, admin=True)
        self.assertEqual(r.status_code, 404)


class ScheduleDataTests(SchedTestBase):
    def test_owner_scoping(self):
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data", email="alice@x").get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a"])
        rows = self.get("/admin/vm-schedule/data", admin=True).get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a", "b"])

    def test_plain_user_only_sees_own_hosts(self):
        # alice's VM is on nv2, bob's on nv3, and nv4 is a pool-only host.
        # A plain user should only see the host block their own VMs are on.
        self.vm("a", owner="alice@x", host="nv2")
        self.vm("b", owner="bob@x", host="nv3")
        self.state("nv2", pool=2)
        self.state("nv3", pool=4)
        self.state("nv4", pool=8)                     # no VM at all
        hosts = self.get("/admin/vm-schedule/data",
                         email="alice@x").get_json()["hosts"]
        self.assertEqual([h["host"] for h in hosts], ["nv2"])
        # Managers (admins included) keep the full roster for capacity
        # questions.
        hosts = self.get("/admin/vm-schedule/data",
                         admin=True).get_json()["hosts"]
        self.assertEqual({h["host"] for h in hosts},
                         {"nv2", "nv3", "nv4"})

    def test_pending_stop_and_has_desired(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "off")
        self.state("nv2", instances=[{"name": "v", "status": "Running"}])
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["pending"], "stop")
        self.assertTrue(row["has_desired"])

    def test_pending_start(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "on")
        self.state("nv2", instances=[{"name": "v", "status": "Stopped"}])
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["pending"], "start")

    def test_no_record_has_desired_false(self):
        self.vm("v", owner="alice@x")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertFalse(row["has_desired"])
        self.assertEqual(row["mode"], "unmanaged")

    def test_grace_seconds_exposed(self):
        d = self.get("/admin/vm-schedule/data").get_json()
        self.assertEqual(d["grace_seconds"], vmm.SCHEDULED_STOP_GRACE_S)

    def test_suggestions_available_for_gpu_vm(self):
        self.state("nv2", pool=4)
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        row = self.get("/admin/vm-schedule/data",
                       email="alice@x").get_json()["rows"][0]
        self.assertGreater(len(row["suggestions"]), 0)
        for s in row["suggestions"]:
            self.assertGreaterEqual(s["hours"], 1)
            self.assertLessEqual(s["hours"], 6)
            self.assertGreater(datetime.fromisoformat(s["start"]),
                               datetime.now(UTC))

    def test_suggestions_ignore_own_unmanaged_commitment(self):
        # No desired record => unmanaged (always-held). The suggestion must
        # not count the VM's own hold against itself; admission does the same
        # when the owner switches it to 'schedule'.
        self.state("nv2", pool=2)
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertGreater(len(row["suggestions"]), 0)

    def test_suggestions_empty_without_pool_or_overcommit(self):
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["suggestions"], [])

        self.state("nv2", pool=2)
        self.vm("other", owner="bob@x", gpus=2, host="nv2")
        self.desired("other", "on")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["suggestions"], [])

    def test_suggestions_account_for_gpu_blocks(self):
        self.state("nv2", pool=2)
        r = self.post("/admin/vm-schedule/gpu-block",
                      {"host": "nv2", "gpus": 2, "mode": "on"}, admin=True)
        self.assertEqual(r.status_code, 200)
        self.vm("mine", owner="alice@x", gpus=2, host="nv2")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["suggestions"], [])

    def test_etcd_down_returns_503(self):
        def boom():
            raise RuntimeError("no etcd")
        vmm.get_etcd_client = boom
        self.assertEqual(self.get("/admin/vm-schedule/data").status_code, 503)


    def test_sched_gpus_exposed(self):
        self.vm("v", owner="alice@x", gpus=4, host="nv2")
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["sched_gpus"], 4)          # default = all registered
        rec = {"mode": "schedule", "gpus": 2, "windows": []}
        self.etcd.seed_json(vmm.VM_DESIRED_PREFIX + "v", rec)
        row = self.get("/admin/vm-schedule/data").get_json()["rows"][0]
        self.assertEqual(row["sched_gpus"], 2)

    def test_manager_with_no_projects_sees_no_vms(self):
        # A manager with no authorized projects sees no schedule rows at all
        # (authorization defaults to none).
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: []
        am.user_project = lambda email: "demo"
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data", manager=True,
                        email="carol@x").get_json()["rows"]
        self.assertEqual(rows, [])

    def test_manager_sees_only_authorized_project_vms(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "demo" if email == "alice@x" else "other"
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data", manager=True,
                        email="carol@x").get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a"])

    def test_manager_hosts_and_owners_scoped_to_authorized_vms(self):
        # The manager's schedule page shows only hosts that hold an
        # authorized-project VM, and only in-scope owners in the capacity
        # panel — out-of-scope owners/hosts don't leak.
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "demo" if email == "alice@x" else "other"
        self.vm("a", owner="alice@x", host="nv2", gpus=2)
        self.vm("b", owner="bob@x", host="nv3", gpus=2)
        self.desired("a", "on")
        self.desired("b", "on")
        self.state("nv2", pool=4)
        self.state("nv3", pool=4)
        d = self.get("/admin/vm-schedule/data", manager=True,
                     email="carol@x").get_json()
        self.assertEqual([h["host"] for h in d["hosts"]], ["nv2"])
        owners = [c["owner"] for h in d["hosts"] for c in h["commitments"]]
        self.assertEqual(owners, ["alice@x"])


    def test_manager_orphan_owner_does_not_500(self):
        # A VM whose owner has no IdP account must not 500 the whole page:
        # the orphan is out of scope for a manager, and authorized VMs still
        # render.
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]

        def user_project(email):
            if email == "ghost@x":
                raise RuntimeError("no account for ghost@x")
            return "demo" if email == "alice@x" else "other"
        am.user_project = user_project
        self.vm("a", owner="alice@x")
        self.vm("g", owner="ghost@x")
        resp = self.get("/admin/vm-schedule/data", manager=True, email="carol@x")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a"])


class ResourcesDataTests(SchedTestBase):
    """Tests for /admin/resources/data — per-compute-host models + VMs."""

    def setUp(self):
        super().setUp()
        # The resources endpoint reads etcd via get_all_hosts() (app module)
        # and inventory.list_all() (inventory module), which each import
        # get_etcd_client separately. Patch all three so the FakeEtcd is
        # used everywhere.
        import ycluster.utils.inventory as inv_mod
        self._orig_app_etcd = appmod.get_etcd_client
        self._orig_inv_etcd = inv_mod.get_etcd_client
        appmod.get_etcd_client = lambda: self.etcd
        inv_mod.get_etcd_client = lambda: self.etcd
        # Mock get_inference_status so no HTTP call is made.
        self._orig_inference = appmod.get_inference_status
        appmod.get_inference_status = lambda: None

    def tearDown(self):
        import ycluster.utils.inventory as inv_mod
        appmod.get_etcd_client = self._orig_app_etcd
        inv_mod.get_etcd_client = self._orig_inv_etcd
        appmod.get_inference_status = self._orig_inference
        super().tearDown()

    def _seed_host(self, hostname, htype, ip, disabled=False):
        """Seed a node allocation in etcd."""
        self.etcd.seed_json(
            f"/cluster/nodes/by-hostname/{hostname}",
            {"hostname": hostname, "type": htype, "ip": ip,
             "mac": "00:00:00:00:00:00", "disabled": disabled})

    def _seed_hardware(self, hostname, gpus=None, cpu="x86_64", ram_gb=64):
        """Seed hardware facts in etcd."""
        self.etcd.seed_json(
            f"/cluster/nodes/hardware/{hostname}",
            {"gpus": gpus or [], "cpu": cpu, "ram_gb": ram_gb})

    def test_only_compute_hosts_returned(self):
        self._seed_host("s1", "storage", "10.0.0.11")
        self._seed_host("c1", "compute", "10.0.0.51")
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        data = self.get("/admin/resources/data").get_json()
        hostnames = [h["hostname"] for h in data["hosts"]]
        self.assertIn("c1", hostnames)
        self.assertIn("nv1", hostnames)
        self.assertNotIn("s1", hostnames)

    def test_vms_grouped_by_host(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self._seed_host("c1", "compute", "10.0.0.51")
        self.vm("vm-a", owner="alice@x", gpus=2, host="nv1")
        self.vm("vm-b", owner="bob@x", gpus=1, host="c1")
        data = self.get("/admin/resources/data").get_json()
        by_host = {h["hostname"]: [v["name"] for v in h["vms"]]
                   for h in data["hosts"]}
        self.assertEqual(sorted(by_host["nv1"]), ["vm-a"])
        self.assertEqual(sorted(by_host["c1"]), ["vm-b"])

    def test_gpu_count_from_hardware(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self._seed_hardware("nv1", gpus=[
            {"vendor": "NVIDIA", "model": "H100", "vram_mb": 81920},
            {"vendor": "NVIDIA", "model": "H100", "vram_mb": 81920},
        ])
        data = self.get("/admin/resources/data").get_json()
        host = [h for h in data["hosts"] if h["hostname"] == "nv1"][0]
        self.assertEqual(host["gpu_count"], 2)

    def test_scheduled_now_on(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", gpus=1, host="nv1")
        self.desired("v", "on")
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertTrue(vm["scheduled_now"])

    def test_scheduled_now_off(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", gpus=1, host="nv1")
        self.desired("v", "off")
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertFalse(vm["scheduled_now"])

    def test_disabled_host_flagged(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self._seed_host("nv2", "nvidia", "10.0.0.112", disabled=True)
        data = self.get("/admin/resources/data").get_json()
        by_name = {h["hostname"]: h for h in data["hosts"]}
        self.assertFalse(by_name["nv1"]["disabled"])
        self.assertTrue(by_name["nv2"]["disabled"])

    def test_sched_gpus_defaults_to_registered(self):
        # Launched with 2 GPUs, but no desired record yet → scheduled GPU
        # count falls back to the registered (max) allocation.
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", gpus=2, host="nv1")
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertEqual(vm["gpus"], 2)
        self.assertEqual(vm["sched_gpus"], 2)

    def test_sched_gpus_uses_desired_record(self):
        # Launched with 4 GPUs but scheduled to run with 1 → the badge shows
        # the currently scheduled count, not the registered max.
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", gpus=4, host="nv1")
        self.desired("v", "on", gpus=1)
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertEqual(vm["gpus"], 4)
        self.assertEqual(vm["sched_gpus"], 1)

    def test_vm_cpu_mem_exposed(self):
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", host="nv1", cpu="8", mem="32GiB")
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertEqual(vm["cpu"], "8")
        self.assertEqual(vm["mem"], "32GiB")

    def test_vm_cpu_mem_optional(self):
        # Pre-existing VMs (launched before vm_launch persisted limits)
        # have no cpu/mem in the record — endpoint returns None, template
        # hides the badges via x-show.
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("v", owner="alice@x", host="nv1")
        data = self.get("/admin/resources/data").get_json()
        vm = data["hosts"][0]["vms"][0]
        self.assertIsNone(vm["cpu"])
        self.assertIsNone(vm["mem"])

    def test_inference_status_included(self):
        appmod.get_inference_status = lambda: {"status": "ok", "models": []}
        data = self.get("/admin/resources/data").get_json()
        self.assertEqual(data["inference"]["status"], "ok")

    def test_manager_with_no_projects_sees_no_vms(self):
        # A manager with no authorized projects sees no VMs on the Resources
        # page (authorization defaults to none).
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: []
        am.user_project = lambda email: "demo"
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("a", owner="alice@x", host="nv1")
        self.vm("b", owner="bob@x", host="nv1")
        data = self.get("/admin/resources/data", manager=True,
                        email="carol@x").get_json()
        vms = [v for h in data["hosts"] for v in h["vms"]]
        self.assertEqual(vms, [])

    def test_manager_sees_only_authorized_project_vms(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "demo" if email == "alice@x" else "other"
        self._seed_host("nv1", "nvidia", "10.0.0.111")
        self.vm("a", owner="alice@x", host="nv1")
        self.vm("b", owner="bob@x", host="nv1")
        data = self.get("/admin/resources/data", manager=True,
                        email="carol@x").get_json()
        vms = [v["name"] for h in data["hosts"] for v in h["vms"]]
        self.assertEqual(vms, ["a"])


class ManagerRoleTests(SchedTestBase):
    """ycluster-managers: view-only counterpart to ycluster-admins.

    Managers see all VMs (with owner emails un-redacted), the inventory
    table, and the user roster — but every mutation endpoint returns 403.
    Admins implicitly pass every manager check; plain users do not."""

    def setUp(self):
        super().setUp()
        # Mirror ResourcesDataTests's setUp so /admin/resources/data works
        # without a real etcd or HTTP inference lookup.
        import ycluster.utils.inventory as inv_mod
        appmod.get_etcd_client = lambda: self.etcd
        inv_mod.get_etcd_client = lambda: self.etcd
        appmod.get_inference_status = lambda: None
        # Managers are now project-scoped (authorized projects default to
        # none). Stub the IdP projections so these role tests don't touch a
        # real IdP: authorizing both demo + other keeps the "manager sees the
        # full in-scope roster" flavor below while remaining project-scoped.
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo", "other"]
        am.user_project = lambda email: {
            "alice@x": "demo", "bob@x": "other"}.get(email, "demo")

    def _seed_host(self, hostname, htype, ip):
        self.etcd.seed_json(
            f"/cluster/nodes/by-hostname/{hostname}",
            {"hostname": hostname, "type": htype, "ip": ip,
             "mac": "00:00:00:00:00:00"})

    # --- VM schedule: data visibility ---
    def test_manager_sees_all_vms(self):
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data",
                        manager=True, email="carol@x").get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a", "b"])

    def test_manager_sees_owner_emails(self):
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data",
                        manager=True, email="carol@x").get_json()["rows"]
        owners = {r["vm"]: r["owner"] for r in rows}
        self.assertEqual(owners, {"a": "alice@x", "b": "bob@x"})

    def test_manager_sees_commitment_owners(self):
        self.vm("a", owner="alice@x", gpus=2, host="nv2")
        self.desired("a", "on")
        d = self.get("/admin/vm-schedule/data", manager=True).get_json()
        commits = d["hosts"][0]["commitments"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["owner"], "alice@x")     # not redacted

    def test_plain_user_redaction_unchanged(self):
        # Sanity: the pre-existing user-only view is unaffected — managers
        # are additive, not a regression for plain users.
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data", email="alice@x").get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a"])     # bob's still hidden

    def test_data_response_flags(self):
        d = self.get("/admin/vm-schedule/data", manager=True).get_json()
        self.assertTrue(d["is_manager"])
        self.assertFalse(d["is_admin"])

    def test_model_usage_hidden_from_managers(self):
        # Model Usage (page + data + options) is admin-only; managers get 403
        # before any DB work. Admins still reach the page shell.
        for path in ("/admin/model-usage",
                     "/admin/model-usage/data",
                     "/admin/model-usage/options"):
            r = self.get(path, manager=True, email="carol@x")
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(self.get("/admin/model-usage", admin=True).status_code, 200)

    def test_nav_identity_reflects_role(self):
        d = self.get("/admin/nav-identity", manager=True, email="carol@x").get_json()
        self.assertFalse(d["is_admin"])
        self.assertTrue(d["is_manager"])
        d = self.get("/admin/nav-identity", admin=True).get_json()
        self.assertTrue(d["is_admin"])
        self.assertTrue(d["is_manager"])

    # --- VM schedule: mutations blocked ---
    def test_manager_cannot_set_others_vm(self):
        # Manager viewing other users' VMs but can't edit them.
        self.vm("a", owner="alice@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "a", "mode": "off"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_can_set_own_vm(self):
        # Manager gets the same owner-edit rights as a plain user.
        self.vm("a", owner="carol@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "a", "mode": "off"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)

    def test_manager_can_set_own_vm_to_schedule(self):
        self.vm("a", owner="carol@x")
        base = datetime.now(UTC) + timedelta(days=2)
        s = base.replace(hour=10, minute=0, second=0, microsecond=0)
        e = base.replace(hour=20, minute=0, second=0, microsecond=0)
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "a", "mode": "schedule",
                       "windows": [{"start": s.isoformat(), "end": e.isoformat()}]},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)

    def test_manager_cannot_set_unmanaged_on_own_vm(self):
        # 'unmanaged' is admin-only even for managers on their own VMs.
        self.vm("a", owner="carol@x")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "a", "mode": "unmanaged"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_cannot_set_unmanaged_on_others_vm(self):
        self.vm("a", owner="alice@x")
        r = self.post("/admin/vm-schedule/set",
                      {"vm": "a", "mode": "unmanaged"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_cannot_change_admin_set_unmanaged(self):
        # Same lock as plain user: admin-set unmanaged VM is locked to
        # its owner, even if the owner is a manager.
        self.vm("a", owner="carol@x")
        self.desired("a", "unmanaged")
        r = self.post("/admin/vm-schedule/set", {"vm": "a", "mode": "off"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_cannot_stop_now_others_vm(self):
        self.vm("a", owner="alice@x")
        self.desired("a", "off")
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "a"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_can_stop_now_own_vm(self):
        # Stop-now needs a pending scheduled stop. Set mode='off' on a
        # VM the manager owns — same setup as test_pending_stop_stamps_immediate.
        self.vm("a", owner="carol@x")
        self.desired("a", "off")
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "a"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)

    # --- inventory ---
    def test_manager_cannot_edit_asset(self):
        r = self.post("/api/inventory/asset/s1", {"location": "rack 7"},
                      manager=True)
        self.assertEqual(r.status_code, 403)

    def test_admin_can_edit_asset(self):
        # Sanity: the admin path is unchanged.
        r = self.post("/api/inventory/asset/s1", {"location": "rack 7"},
                      admin=True)
        self.assertEqual(r.status_code, 200)

    # --- /admin/users/data ---
    def test_manager_with_no_projects_sees_empty_roster(self):
        # Manager with no authorized projects sees no users — the roster is
        # project-scoped and defaults to none. We stub authentik_manager to
        # return canned data — no real IdP in unit tests.
        from ycluster.utils import authentik_manager as am
        am.users_data = lambda: [{"email": "alice@x", "name": "Alice",
                                  "type": "internal", "active": True,
                                  "last_login": None, "date_joined": None,
                                  "project_name": "", "admin_notes": "",
                                  "is_admin": False, "is_manager": False}]
        am.invitations_data = lambda projects=None: []
        am.manager_projects = lambda email: []
        d = self.get("/admin/users/data", manager=True).get_json()
        # is_admin in the response is false for a manager caller; the page
        # uses this to hide admin-only controls. With no authorized projects
        # the roster is empty, as is the invite project selector.
        self.assertFalse(d["is_admin"])
        self.assertEqual(d["manager_projects"], [])
        self.assertEqual(d["users"], [])

    def test_manager_sees_only_authorized_project_users(self):
        from ycluster.utils import authentik_manager as am
        am.users_data = lambda: [
            {"email": "alice@x", "name": "Alice", "type": "internal",
             "active": True, "last_login": None, "date_joined": None,
             "project_name": "demo", "admin_notes": "",
             "is_admin": False, "is_manager": False},
            {"email": "bob@x", "name": "Bob", "type": "internal",
             "active": True, "last_login": None, "date_joined": None,
             "project_name": "other", "admin_notes": "",
             "is_admin": False, "is_manager": False},
        ]
        am.invitations_data = lambda projects=None: []
        am.manager_projects = lambda email: ["demo"]
        d = self.get("/admin/users/data", manager=True).get_json()
        self.assertEqual([u["email"] for u in d["users"]], ["alice@x"])
        # Every visible row is one the manager may manage.
        self.assertTrue(all(u["can_manage"] for u in d["users"]))

    def test_admin_user_data_includes_is_admin_true(self):
        from ycluster.utils import authentik_manager as am
        am.users_data = lambda: []
        am.invitations_data = lambda: []
        d = self.get("/admin/users/data", admin=True).get_json()
        self.assertTrue(d["is_admin"])

    def test_plain_user_cannot_list_users(self):
        d = self.get("/admin/users/data", email="alice@x").get_json()
        self.assertEqual(d, {"error": "admin only"})

    def test_plain_user_cannot_act_on_user(self):
        # Plain users (no manager/admin group) get 403 before any IdP call.
        for action in ('invite', 'edit', 'recovery', 'uninvite',
                       'admin_add', 'admin_remove', 'manager_add',
                       'manager_remove', 'activate', 'deactivate'):
            r = self.post("/admin/users/action",
                          {"action": action, "email": "bob@x.org"},
                          email="carol@x")
            self.assertEqual(r.status_code, 403,
                             f"action {action!r} should be 403 for plain user")

    def test_manager_can_edit_notes_in_authorized_project(self):
        # Manager edit forwards ONLY the notes field (never name/project)
        # and only when the target user's project is authorized.
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "demo" if email == "bob@x.org" else ""
        am.update_user = lambda email, name=None, project_name=None, admin_notes=None: (
            captured.update(email=email, name=name, project_name=project_name,
                            admin_notes=admin_notes) or f"updated {email}")
        r = self.post("/admin/users/action",
                      {"action": "edit", "email": "bob@x.org",
                       "admin_notes": "VIP note"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured, {"email": "bob@x.org", "name": None,
                                    "project_name": None,
                                    "admin_notes": "VIP note"})

    def test_manager_cannot_edit_notes_outside_authorized_project(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "other"
        am.update_user = lambda *a, **k: (
            (_ for _ in ()).throw(AssertionError("update_user must not run")))
        r = self.post("/admin/users/action",
                      {"action": "edit", "email": "bob@x.org",
                       "admin_notes": "x"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_can_invite_into_authorized_project(self):
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.manager_projects = lambda email: ["demo"]
        am.invite_user = lambda email, name, days, project=None: (
            captured.update(email=email, name=name, days=days, project=project)
            or "http://invite")
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "new@x.org",
                       "project_name": "demo", "days": 7},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)
        # The invitation is tagged with the chosen project.
        self.assertEqual(captured, {"email": "new@x.org", "name": None,
                                    "days": 7, "project": "demo"})

    def test_manager_cannot_invite_without_or_unauthed_project(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.invite_user = lambda *a, **k: (
            (_ for _ in ()).throw(AssertionError("invite_user must not run")))
        # No project supplied.
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "new@x.org"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)
        # Project not in the manager's authorized set.
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "new@x.org",
                       "project_name": "other"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_can_recovery_authorized_user(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "demo"
        am.recovery_link = lambda email: "http://reset"
        r = self.post("/admin/users/action",
                      {"action": "recovery", "email": "bob@x.org"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["url"], "http://reset")

    def test_manager_cannot_recovery_outside_authorized_project(self):
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        am.user_project = lambda email: "other"
        am.recovery_link = lambda *a, **k: (
            (_ for _ in ()).throw(AssertionError("recovery_link must not run")))
        r = self.post("/admin/users/action",
                      {"action": "recovery", "email": "bob@x.org"},
                      manager=True, email="carol@x")
        self.assertEqual(r.status_code, 403)

    def test_manager_sees_only_authorized_project_invitations(self):
        from ycluster.utils import authentik_manager as am
        am.users_data = lambda: []
        am.manager_projects = lambda email: ["demo"]
        seen = {}
        def invitations_data(projects=None):
            seen["projects"] = projects
            return [{"email": "a@x.org", "project_name": "demo",
                     "expires": None, "url": "u1"}]
        am.invitations_data = invitations_data
        d = self.get("/admin/users/data", manager=True,
                     email="carol@x").get_json()
        self.assertEqual(seen["projects"], {"demo"})
        self.assertEqual(d["invitations"][0]["project_name"], "demo")

    def test_manager_still_403_on_admin_only_actions(self):
        # Role changes, enable/disable, and invitation revocation stay
        # admin-only even for managers with authorized projects.
        from ycluster.utils import authentik_manager as am
        am.manager_projects = lambda email: ["demo"]
        for action in ('uninvite', 'admin_add', 'admin_remove',
                       'manager_add', 'manager_remove', 'activate',
                       'deactivate'):
            r = self.post("/admin/users/action",
                          {"action": action, "email": "bob@x.org"},
                          manager=True, email="carol@x")
            self.assertEqual(r.status_code, 403,
                             f"action {action!r} should be 403 for manager")

    def test_admin_can_invite_with_optional_project(self):
        # Admins keep the existing invite path: project is optional and
        # passed through when supplied.
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.invite_user = lambda email, name, days, project=None: (
            captured.update(email=email, name=name, days=days, project=project)
            or "http://invite")
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "new@x.org",
                       "project_name": "demo"},
                      admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["project"], "demo")
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "new2@x.org"},
                      admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(captured["project"])

    def test_admin_edit_user_with_project_and_notes(self):
        # Edit action forwards project_name and admin_notes through to
        # authentik_manager.update_user(); omitting either keeps it
        # unchanged (None means "don't touch this field").
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.update_user = lambda email, name=None, project_name=None, admin_notes=None: (
            captured.update(email=email, name=name,
                            project_name=project_name,
                            admin_notes=admin_notes)
            or f"updated {email}")
        r = self.post("/admin/users/action",
                      {"action": "edit", "email": "bob@x.org",
                       "name": "Bob", "project_name": "demo",
                       "admin_notes": "VIP customer"},
                      admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured, {"email": "bob@x.org", "name": "Bob",
                                    "project_name": "demo",
                                    "admin_notes": "VIP customer"})

    def test_admin_edit_user_omits_optional_fields(self):
        # An edit request without project_name/admin_notes must not touch
        # them (None = skip in update_user) — only the provided fields
        # change.
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.update_user = lambda email, name=None, project_name=None, admin_notes=None: (
            captured.update(email=email, name=name,
                            project_name=project_name,
                            admin_notes=admin_notes)
            or f"updated {email}")
        r = self.post("/admin/users/action",
                      {"action": "edit", "email": "bob@x.org",
                       "name": "Bob"},
                      admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(captured["project_name"])
        self.assertIsNone(captured["admin_notes"])

    def test_users_data_includes_metadata_fields(self):
        # users_data() (and therefore /admin/users/data) carries
        # date_joined, project_name, admin_notes for the page to render.
        from ycluster.utils import authentik_manager as am
        am.users_data = lambda: [{"email": "alice@x", "name": "Alice",
                                  "type": "internal", "active": True,
                                  "last_login": None,
                                  "date_joined": "2026-01-02T03:04:05Z",
                                  "project_name": "demo",
                                  "admin_notes": "VIP",
                                  "is_admin": False, "is_manager": False}]
        am.invitations_data = lambda: []
        d = self.get("/admin/users/data", admin=True).get_json()
        u = d["users"][0]
        self.assertEqual(u["date_joined"], "2026-01-02T03:04:05Z")
        self.assertEqual(u["project_name"], "demo")
        self.assertEqual(u["admin_notes"], "VIP")

    # --- resources data response carries both flags ---
    def test_resources_data_flags(self):
        self._seed_host("c1", "compute", "10.0.0.51")
        d = self.get("/admin/resources/data", manager=True).get_json()
        self.assertTrue(d["is_manager"])
        self.assertFalse(d["is_admin"])

    # --- inventory data response carries both flags ---
    def test_inventory_data_flags(self):
        # Inventory returns no rows in unit tests; just check the flags.
        d = self.get("/api/inventory", manager=True).get_json()
        self.assertTrue(d["is_manager"])
        self.assertFalse(d["is_admin"])


class LoopbackGuardTests(SchedTestBase):
    """Belt-and-suspenders: every identity check requires that the
    TCP source is loopback. admin-api listens on 0.0.0.0:12723, so a
    remote cluster node could otherwise hit the Flask app directly and
    pass as admin via the "no headers = operator space" shortcut."""

    def setUp(self):
        super().setUp()
        # Stub authentik_manager so /admin/users/data tests don't hit
        # the real IdP API (not available in unit tests).
        from ycluster.utils import authentik_manager as am
        self._orig_users_data = am.users_data
        self._orig_invitations_data = am.invitations_data
        self._orig_update_user = am.update_user
        am.users_data = lambda: [{"email": "alice@x", "name": "Alice",
                                  "type": "internal", "active": True,
                                  "last_login": None, "date_joined": None,
                                  "project_name": "", "admin_notes": "",
                                  "is_admin": False, "is_manager": False}]
        am.invitations_data = lambda: []
        # Patch inventory's etcd client too — /api/inventory reads via
        # ycluster.utils.inventory which imports get_etcd_client separately
        # from vm_manager and app.py (same caveat as ResourcesDataTests).
        import ycluster.utils.inventory as inv_mod
        self._orig_inv_etcd = inv_mod.get_etcd_client
        inv_mod.get_etcd_client = lambda: self.etcd

    def tearDown(self):
        from ycluster.utils import authentik_manager as am
        am.users_data = self._orig_users_data
        am.invitations_data = self._orig_invitations_data
        am.update_user = self._orig_update_user
        import ycluster.utils.inventory as inv_mod
        inv_mod.get_etcd_client = self._orig_inv_etcd
        super().tearDown()

    # --- /admin/users/data is a clean probe (returns is_admin) ---
    def test_loopback_headerless_is_admin(self):
        # No headers, 127.0.0.1 → admin (operator space, the original design).
        # admin=True in our request helper means "no X-Authentik-* headers".
        d = self.get("/admin/users/data", admin=True,
                     remote_addr="127.0.0.1").get_json()
        self.assertTrue(d["is_admin"])

    def test_headerless_from_127_0_0_2_not_admin(self):
        # Belt-and-suspenders tightening: the "no headers → admin" shortcut
        # is the dangerous path — it requires the caller to be EXACTLY
        # 127.0.0.1, not just any loopback. nginx's upstream from the
        # public frp vhost (listen 127.0.0.2:443) may arrive
        # here; if forward-auth was bypassed, the request would be
        # headerless, and we don't want it treated as admin.
        d = self.get("/admin/users/data", admin=True,
                     remote_addr="127.0.0.2").get_json()
        self.assertEqual(d, {"error": "admin only"})

    def test_non_loopback_headerless_not_admin(self):
        # A remote cluster node hitting admin-api directly with no
        # forward-auth headers is NOT admin.
        d = self.get("/admin/users/data", admin=True,
                     remote_addr="10.0.0.51").get_json()
        self.assertEqual(d, {"error": "admin only"})

    def test_non_loopback_with_admin_headers_not_admin(self):
        # Even if a non-loopback caller somehow smuggles admin headers
        # (e.g. forged X-Authentik-Groups), the loopback guard denies.
        # We can't reach /admin/users/data here (it'd 403), so probe via
        # /api/inventory which is open to any authed user.
        d = self.get("/api/inventory", remote_addr="10.0.0.51",
                     admin=True).get_json()
        self.assertFalse(d["is_admin"])

    def test_loopback_with_admin_headers_is_admin(self):
        # Forward-auth via the public vhost: nginx proxies from loopback
        # with X-Authentik-Groups=ycluster-admins set.
        d = self.get("/admin/users/data", remote_addr="127.0.0.1",
                     admin=True).get_json()
        self.assertTrue(d["is_admin"])

    def test_group_admin_without_manager_group_can_act_on_user(self):
        # A forward-auth admin in ycluster-admins but NOT ycluster-managers
        # must still pass users_action's is_manager gate (admins implicitly
        # pass the manager check) and retain full user-mutation rights.
        from ycluster.utils import authentik_manager as am
        captured = {}
        am.update_user = lambda email, name=None, project_name=None, admin_notes=None: (
            captured.update(email=email, name=name, project_name=project_name,
                            admin_notes=admin_notes) or f"updated {email}")
        r = self.http.post("/admin/users/action",
                           json={"action": "edit", "email": "bob@x.org",
                                 "name": "Bob", "admin_notes": "note"},
                           headers={"X-Authentik-Email": "alice@x",
                                    "X-Authentik-UID": "uid-alice",
                                    "X-Authentik-Groups": "ycluster-admins"},
                           environ_overrides={"REMOTE_ADDR": "127.0.0.2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["email"], "bob@x.org")

    def test_forward_auth_from_127_0_0_2_with_admin_headers_is_admin(self):
        # nginx upstream from the public frp vhost may arrive
        # on 127.0.0.2; the forward-auth group check accepts any loopback
        # (the headers were set by nginx, so we trust them).
        # admin=True in our helper = "no headers" — instead set headers
        # explicitly to ADMIN_GROUP.
        r = self.http.get("/admin/users/data",
                          headers={"X-Authentik-Email": "alice@x",
                                   "X-Authentik-UID": "uid-alice",
                                   "X-Authentik-Groups": "ycluster-admins"},
                          environ_overrides={"REMOTE_ADDR": "127.0.0.2"})
        d = r.get_json()
        self.assertTrue(d["is_admin"])

    def test_oauth_user_without_email_is_not_admin(self):
        # OAuth-sourced accounts (e.g., GitHub users with private emails)
        # have a UID but no email. They must NOT trigger the "no headers
        # → admin" shortcut (email is None), and they must NOT be admin
        # via the group check either (no ycluster-admins group).
        r = self.http.get("/admin/users/data",
                          headers={"X-Authentik-UID": "uid-github-9001",
                                   "X-Authentik-Groups": "users"},
                          environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        d = r.get_json()
        self.assertEqual(d, {"error": "admin only"})

    def test_oauth_user_without_email_with_manager_group_is_manager(self):
        # An OAuth-sourced user granted the manager group should pass
        # the manager check even without email. The UID is the
        # "is this authenticated?" signal — not email.
        d = self.http.get("/api/inventory",
                          headers={"X-Authentik-UID": "uid-github-9001",
                                   "X-Authentik-Groups": "ycluster-managers"},
                          environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_json()
        self.assertTrue(d["is_manager"])
        self.assertFalse(d["is_admin"])

    def test_uid_present_without_email_blocks_opspace_shortcut(self):
        # Belt-and-suspenders specifically for the OAuth-no-email case:
        # a UID without email must NOT be mistaken for an operator-space
        # headerless request. /api/inventory returns is_admin/is_manager
        # and email (which can be null) so we can assert the identity
        # shape directly.
        d = self.http.get("/api/inventory",
                          headers={"X-Authentik-UID": "uid-anon",
                                   "X-Authentik-Groups": "users"},
                          environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_json()
        self.assertFalse(d["is_admin"])
        self.assertFalse(d["is_manager"])
        # email is null in the response (None in the Identity), which
        # is the key signal that this isn't the operator-space shortcut.
        self.assertIsNone(d["email"])

    def test_non_loopback_inventory_mutation_blocked(self):
        # Belt-and-suspenders also blocks mutations from non-loopback
        # callers even when they have admin headers.
        r = self.post("/api/inventory/asset/s1", {"location": "rack 7"},
                      remote_addr="10.0.0.51", admin=True)
        self.assertEqual(r.status_code, 403)

    def test_non_loopback_vm_schedule_set_blocked(self):
        # A remote cluster node with admin headers can't edit schedules.
        self.vm("a", owner="alice@x")
        r = self.post("/admin/vm-schedule/set", {"vm": "a", "mode": "off"},
                      remote_addr="10.0.0.51", admin=True)
        self.assertEqual(r.status_code, 403)

    def test_non_loopback_users_action_blocked(self):
        # Mutations on users likewise blocked from non-loopback.
        r = self.post("/admin/users/action",
                      {"action": "invite", "email": "bob@x"},
                      remote_addr="10.0.0.51", admin=True)
        self.assertEqual(r.status_code, 403)


class AuthentikAPIEmptyBodyTests(unittest.TestCase):
    """Authentik's /core/groups/<pk>/add_user|remove_user endpoints return
    204 No Content on success. AuthentikAPI._request must not raise
    JSONDecodeError on the empty body (regression: surfaced as a 400
    `{"error":"Expecting value: line 1 column 1 (char 0)"}` from
    /admin/users/action while the membership change did land)."""

    def _api(self, resp):
        from unittest.mock import MagicMock
        from ycluster.utils.authentik_manager import AuthentikAPI
        api = AuthentikAPI.__new__(AuthentikAPI)
        api.base = 'http://x/api/v3'
        api.session = MagicMock()
        api.session.request.return_value = resp
        return api

    def test_empty_body_returns_none(self):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 204
        resp.content = b''
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        api = self._api(resp)
        self.assertIsNone(api._request('POST', '/core/groups/x/add_user/'))

    def test_nonempty_body_parses_json(self):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"results": [{"pk": "x"}]}'
        resp.json.return_value = {"results": [{"pk": "x"}]}
        api = self._api(resp)
        self.assertEqual(api._request('GET', '/core/users/'), {"results": [{"pk": "x"}]})


class VMScheduleCanEditTests(SchedTestBase):
    """Server-computed `can_edit` per row in /admin/vm-schedule/data.
    The JS template binds `:disabled="!row.can_edit"` to the mode
    <select> + Save button — so the disable logic is server-authoritative
    and mirrors the rules vm_schedule_set enforces. Regression: the
    manager-role commit added `!isAdmin ||` to the JS binding, which
    disabled the dropdown for ALL non-admin users (including owners
    of their own VMs). Users reported being unable to schedule VMs
    from 'off' to 'on'/'schedule'. These tests assert the API field
    directly — no template rendering."""

    def _row(self, vm, **desired):
        self.vm(vm)
        if desired:
            self.desired(vm, **desired)

    def test_non_admin_owner_off_mode_can_edit(self):
        # Regression case: non-admin owner of 'off' VM must be able to
        # change the mode. Before the fix this was disabled because
        # the JS binding had `!isAdmin ||`.
        self._row('a', mode='off')
        rows = self.get("/admin/vm-schedule/data",
                        email="alice@x").get_json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["can_edit"],
                        "non-admin owner of their own 'off' VM must can_edit=True")

    def test_non_admin_owner_schedule_mode_can_edit(self):
        self._row('a', mode='schedule', windows=[])
        rows = self.get("/admin/vm-schedule/data",
                        email="alice@x").get_json()["rows"]
        self.assertTrue(rows[0]["can_edit"])

    def test_non_admin_owner_no_record_can_edit(self):
        # No desired record (implicit unmanaged) — owner can schedule.
        self.vm('a')
        rows = self.get("/admin/vm-schedule/data",
                        email="alice@x").get_json()["rows"]
        self.assertTrue(rows[0]["can_edit"])

    def test_non_admin_owner_admin_set_unmanaged_cannot_edit(self):
        # Existing behavior: non-admin can't touch admin-set unmanaged.
        self._row('a', mode='unmanaged')
        rows = self.get("/admin/vm-schedule/data",
                        email="alice@x").get_json()["rows"]
        self.assertFalse(rows[0]["can_edit"])
        self.assertTrue(rows[0]["admin_locked"])

    def test_admin_can_edit_anything(self):
        # Admin can edit even admin-set unmanaged.
        self._row('a', mode='unmanaged')
        rows = self.get("/admin/vm-schedule/data", admin=True).get_json()["rows"]
        self.assertTrue(rows[0]["can_edit"])

    def test_manager_can_edit_own_vm(self):
        # Manager gets the same owner-edit rights as a plain user.
        self._row('a', mode='off')
        rows = self.get("/admin/vm-schedule/data",
                        manager=True, email="alice@x").get_json()["rows"]
        self.assertTrue(rows[0]["can_edit"],
                        "manager who owns their own VM must can_edit=True")

    def test_manager_cannot_edit_others_vm(self):
        # Managers see every VM but can only edit their own. The data
        # endpoint still includes the row (manager visibility), but
        # can_edit=False keeps the controls disabled.
        self.vm('a', owner='bob@x')
        rows = self.get("/admin/vm-schedule/data",
                        manager=True, email="alice@x").get_json()["rows"]
        self.assertFalse(rows[0]["can_edit"])

    def test_manager_cannot_edit_own_admin_set_unmanaged(self):
        # Same lock as plain user: admin-set unmanaged VM is locked
        # to its owner even if the owner is a manager.
        self._row('a', mode='unmanaged')
        rows = self.get("/admin/vm-schedule/data",
                        manager=True, email="alice@x").get_json()["rows"]
        self.assertFalse(rows[0]["can_edit"])
        self.assertTrue(rows[0]["admin_locked"])


class ApiKeysAppTests(SchedTestBase):
    """Self-service inference API-key surface (/admin/settings/api-keys).

    Routes are scoped to the caller's own email from the forward-auth
    headers — a user can only ever see/create/revoke their own keys.
    """

    def test_no_auth_user_is_403(self):
        # No forward-auth email (X-Authentik-Email absent) → no user to
        # self-manage; the route is rejected regardless of UID.
        r = self.get("/admin/settings/api-keys/data", email=None)
        self.assertEqual(r.status_code, 403)

    def test_data_lists_own_keys(self):
        via_http = self.post_json("/admin/settings/api-keys/create",
                                  {"label": "laptop"}, email="alice@x")
        self.assertEqual(via_http.status_code, 200)
        d = self.get("/admin/settings/api-keys/data", email="alice@x").get_json()
        self.assertEqual(d["email"], "alice@x")
        self.assertEqual(len(d["keys"]), 1)
        self.assertEqual(d["keys"][0]["label"], "laptop")
        self.assertNotIn("hash", d["keys"][0])
        self.assertNotIn("secret", d["keys"][0])

    def test_create_returns_plaintext_once(self):
        r = self.post_json("/admin/settings/api-keys/create",
                           {"label": "cli"}, email="alice@x")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["secret"].startswith("sk-"))
        # Plaintext never lands in etcd.
        self.assertNotIn(body["secret"], list(self.etcd.store.values()))
        # No re-display: a subsequent data call exposes only metadata.
        d = self.get("/admin/settings/api-keys/data", email="alice@x").get_json()
        self.assertNotIn("secret", str(d))

    def test_create_bad_label_is_400(self):
        r = self.post_json("/admin/settings/api-keys/create",
                           {"label": 123}, email="alice@x")
        self.assertEqual(r.status_code, 400)

    def test_revoke_own_key(self):
        secret = apikeys.create_key("alice@x", label="test", hasher=_fast_hasher())
        key_id = apikeys.list_keys("alice@x")["alice@x"][0]["key_id"]
        r = self.post_json("/admin/settings/api-keys/revoke",
                           {"key_id": key_id}, email="alice@x")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(apikeys.validate_key(secret, hasher=_fast_hasher()))
        self.assertEqual(apikeys.list_keys("alice@x")["alice@x"], [])

    def test_cannot_revoke_another_users_key(self):
        apikeys.create_key("bob@x", label="bob", hasher=_fast_hasher())
        bob_key = apikeys.list_keys("bob@x")["bob@x"][0]["key_id"]
        r = self.post_json("/admin/settings/api-keys/revoke",
                           {"key_id": bob_key}, email="alice@x")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(apikeys.list_keys("bob@x")["bob@x"]), 1)

    def test_revoke_missing_id_is_404(self):
        r = self.post_json("/admin/settings/api-keys/revoke",
                           {"key_id": "does-not-exist"}, email="alice@x")
        self.assertEqual(r.status_code, 404)

    def test_page_renders(self):
        r = self.get("/admin/settings/api-keys", email="alice@x")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"API Keys", r.data)

    # convenience wrapper scoping POSTs to an email
    def post_json(self, path, body, email):
        return self.http.post(path, json=body,
                              headers=self._headers(False, email),
                              environ_overrides=self._environ("127.0.0.1"))


class AppLandingTests(SchedTestBase):
    """Root of <domain> — a landing page linking the user-facing apps.

    Cross-subdomain links (Open WebUI on chat.<domain>, zulip/admin on their
    subdomains) are built from the canonical etcd domain; the Admin link is
    gated on admin/manager identity. Comfy is intentionally hidden from
    everyone for now."""

    def seed_domain(self, domain="example.com"):
        self.etcd.put("/cluster/https/domain", domain)

    def test_landing_renders_for_plain_user(self):
        self.seed_domain()
        r = self.get("/", admin=False, email="alice@x")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Open WebUI", r.data)
        self.assertIn(b"Zulip", r.data)
        # The root's own pages are nav, not apps — not listed here.
        self.assertNotIn(b"VM Schedule", r.data)
        self.assertNotIn(b"Settings", r.data)
        # A plain user is neither admin nor manager -> no Admin link.
        self.assertNotIn(b"YCluster Admin", r.data)

    def test_landing_links_use_seeded_domain(self):
        self.seed_domain("example.com")
        r = self.get("/", admin=False, email="alice@x")
        self.assertIn(b"https://chat.example.com/", r.data)
        self.assertIn(b"https://zulip.example.com/", r.data)

    def test_landing_apps_have_logos(self):
        self.seed_domain("example.com")
        r = self.get("/", admin=True)
        self.assertIn(b'src="/static/open-webui.svg"', r.data)
        self.assertIn(b'src="/static/zulip.svg"', r.data)
        self.assertIn(b'src="/static/ycluster-favicon.svg"', r.data)

    def test_comfy_is_hidden_from_everyone(self):
        # Comfy is intentionally not linked on the landing page yet; it is
        # hidden from all users regardless of role.
        self.seed_domain()
        for kwargs in (dict(admin=True), dict(admin=False, manager=True),
                       dict(admin=False, email="alice@x")):
            r = self.get("/", **kwargs)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn(b"Comfy", r.data)

    def test_admin_sees_admin_link(self):
        self.seed_domain()
        r = self.get("/", admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"YCluster Admin", r.data)
        self.assertIn(b"https://admin.example.com/", r.data)

    def test_manager_sees_admin_link(self):
        self.seed_domain()
        r = self.get("/", admin=False, email="alice@x", manager=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"YCluster Admin", r.data)

    def test_landing_without_domain_renders_empty(self):
        # No /cluster/https/domain and no app. host: no cross-subdomain apps
        # (and thus no Admin link) can be built — the page still renders, and
        # app.<domain>'s own pages remain reachable via the nav bar.
        r = self.get("/", admin=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"YCluster Admin", r.data)


if __name__ == "__main__":
    unittest.main()
