"""Unit tests for VM usage snapshot ingestion."""

import importlib.util
import json
import sys
import types
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The collector imports the real DB driver at module scope; tests only
# exercise etcd/row assembly, so provide the import-time module seam.
sys.modules.setdefault("psycopg2", types.ModuleType("psycopg2"))

_FILES = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_FILES / "ycluster"))
_spec = importlib.util.spec_from_file_location(
    "collect_vm_stats", _FILES / "collect-vm-stats.py")
collect_vm_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect_vm_stats)


class FakeCursor:
    def __init__(self, parent):
        self.parent = parent

    def executemany(self, _sql, rows):
        self.parent.rows.extend(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self):
        self.rows = []
        self.transactions = 0

    def cursor(self):
        return FakeCursor(self)

    def __enter__(self):
        self.transactions += 1
        return self

    def __exit__(self, *args):
        return False


class FakeMeta:
    def __init__(self, key):
        self.key = key


class FakeClient:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def get_prefix(self, _prefix):
        return self.snapshots


def snapshot(vm, ts, gpus=2, status='Running', interval=120):
    return {
        'instances': [{'name': vm, 'gpus': gpus, 'status': status}],
        'ts': ts.isoformat(),
        'interval_s': interval,
    }


class IngestSnapshotsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        stale = collect_vm_stats.SNAPSHOT_MAX_AGE + timedelta(seconds=1)
        self.client = FakeClient([
            (None, FakeMeta(b'/cluster/vm-state/empty')),
            (b'not-json', FakeMeta(b'/cluster/vm-state/bad-host')),
            (json.dumps(snapshot('stale', self.now - stale)).encode(),
             FakeMeta(b'/cluster/vm-state/stale-host')),
            (json.dumps(snapshot('vm-a', self.now, interval=60)).encode(),
             FakeMeta(b'/cluster/vm-state/host-a')),
        ])

    def test_ingests_fresh_rows_with_registration_metadata(self):
        conn = FakeConn()
        with unittest.mock.patch.object(
                collect_vm_stats, 'vm_registrations',
                return_value={'vm-a': {'owner': 'alice'}}):
            count = collect_vm_stats.ingest_snapshots(self.client, conn)
        self.assertEqual(count, 1)
        self.assertEqual(conn.transactions, 1)
        self.assertEqual(len(conn.rows), 1)
        ts, vm, host, owner, gpus, state, interval = conn.rows[0]
        self.assertEqual(vm, 'vm-a')
        self.assertEqual(host, 'host-a')
        self.assertEqual(owner, 'alice')
        self.assertEqual((gpus, state, interval), (2, 'Running', 60))
        self.assertEqual(ts.tzinfo, timezone.utc)

    def test_no_rows_does_not_open_transaction(self):
        self.client.snapshots = []
        conn = FakeConn()
        self.assertEqual(collect_vm_stats.ingest_snapshots(self.client, conn), 0)
        self.assertEqual(conn.transactions, 0)


if __name__ == '__main__':
    unittest.main()
