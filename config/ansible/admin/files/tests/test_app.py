"""Web-app tests for the admin VM-schedule endpoints.

Drives the Flask app through its test client with a dict-backed fake etcd
patched over vm_manager's DAL (get_etcd_client). No etcd/postgres server,
no DB drivers — psycopg2 is a lazy import the schedule endpoints don't
touch, and the etcd client is faked at the DAL seam.

Run from config/ansible/admin/files:
    PYTHONPATH=.:ycluster python3 -m unittest discover -s tests
(or via ./run-tests.sh at the repo root, which also runs the package tests).
"""

import os
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
from ycluster.utils import vm_manager as vmm             # noqa: E402

UTC = timezone.utc


def _iso(dt):
    return dt.isoformat()


class SchedTestBase(unittest.TestCase):
    def setUp(self):
        self.etcd = FakeEtcd()
        self._orig = vmm.get_etcd_client
        vmm.get_etcd_client = lambda: self.etcd
        self.http = appmod.app.test_client()

    def tearDown(self):
        vmm.get_etcd_client = self._orig

    # --- seeding ---
    def vm(self, name, owner="alice@x", gpus=0, host="nv2", type="vm"):
        self.etcd.seed_json(vmm.VMS_PREFIX + name,
                            {"owner": owner, "gpus": gpus, "host": host, "type": type})

    def desired(self, name, mode, windows=None):
        self.etcd.seed_json(vmm.VM_DESIRED_PREFIX + name,
                            {"mode": mode, "windows": windows or []})

    def state(self, host, pool=None, instances=None):
        snap = {"instances": instances or []}
        if pool is not None:
            snap["gpu_pool"] = pool
        self.etcd.seed_json(vmm.VM_STATE_PREFIX + host, snap)

    # --- requests (admin = internal, no forward-auth headers) ---
    def _headers(self, admin, email):
        return {} if admin else {"X-Authentik-Email": email,
                                 "X-Authentik-Groups": "users"}

    def post(self, path, body, admin=False, email="alice@x"):
        return self.http.post(path, json=body, headers=self._headers(admin, email))

    def get(self, path, admin=False, email="alice@x"):
        return self.http.get(path, headers=self._headers(admin, email))


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
        self.assertEqual(self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")["mode"], "off")

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
        s, e = datetime(2026, 7, 1, 10, tzinfo=UTC), datetime(2026, 7, 1, 14, tzinfo=UTC)
        w = [{"start": _iso(s), "end": _iso(e)}]
        self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "schedule", "windows": w})
        self.post("/admin/vm-schedule/set", {"vm": "v", "mode": "off", "windows": w})
        rec = self.etcd.get_json(vmm.VM_DESIRED_PREFIX + "v")
        self.assertEqual(rec["mode"], "off")
        self.assertEqual(len(rec["windows"]), 1)         # preserved, not cleared

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


class StopNowTests(SchedTestBase):
    def test_pending_stop_stamps_immediate(self):
        self.vm("v", owner="alice@x")
        self.desired("v", "off")                         # wants off -> pending stop
        r = self.post("/admin/vm-schedule/stop-now", {"vm": "v"})
        self.assertEqual(r.status_code, 200)
        g = self.etcd.get_json(vmm.VM_GRACE_PREFIX + "v")
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


class ScheduleDataTests(SchedTestBase):
    def test_owner_scoping(self):
        self.vm("a", owner="alice@x")
        self.vm("b", owner="bob@x")
        rows = self.get("/admin/vm-schedule/data", email="alice@x").get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a"])
        rows = self.get("/admin/vm-schedule/data", admin=True).get_json()["rows"]
        self.assertEqual([r["vm"] for r in rows], ["a", "b"])

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

    def test_etcd_down_returns_503(self):
        def boom():
            raise RuntimeError("no etcd")
        vmm.get_etcd_client = boom
        self.assertEqual(self.get("/admin/vm-schedule/data").status_code, 503)


if __name__ == "__main__":
    unittest.main()
