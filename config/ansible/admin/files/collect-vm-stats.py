#!/usr/bin/env python3
"""
VM usage accounting: drain lifecycle events and sample running instances.

Runs every 2 minutes via systemd timer, leader-only. On each run:
1. Exits early if this node is not the storage leader (mountpoint check)
2. Drains /cluster/vms-events/ (written by vm_manager) into
   usage_stats.vm_events — idempotent via the UNIQUE etcd_key column,
   keys deleted only after the insert commits (crash-safe exactly-once)
3. Turns per-host incus snapshots under /cluster/vm-state/ (pushed by the
   vm-state-sampler timer on incus hosts via `ycluster vm sample` — etcd
   client certs, the cluster's one trust mechanism; the leader never
   reaches into hosts) into usage_stats.vm_samples rows. Stale snapshots
   are skipped, so a dead host/timer is never mistaken for runtime, and
   a UNIQUE(vm, host, ts) guard makes re-reads idempotent.

Events are authoritative for billing (they carry initiator/billable);
samples are the cross-check that makes untracked runtime visible
(manual incus ops, autostart after host reboot, missed events).
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

# Everything this script touches (etcd gRPC, postgres on localhost) is
# cluster-internal; a system-wide proxy env (squid) would break those
# connections, so scrub it here rather than in the unit file — the script
# is what knows its traffic never leaves the cluster.
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_v, None)

import psycopg2
from ycluster.common.etcd_utils import get_etcd_client
# The etcd schema belongs to vm_manager (the writer of these prefixes).
from ycluster.utils.vm_manager import EVENTS_PREFIX, VMS_PREFIX, VM_STATE_PREFIX

ETCD_CREDS_KEY = "/cluster/config/usage_stats/db-password"
# A snapshot older than this is a dead sampler/host, not running VMs.
SNAPSHOT_MAX_AGE = timedelta(minutes=10)


def is_storage_leader():
    return subprocess.call(["mountpoint", "-q", "/rbd/user"]) == 0


def get_db(client):
    password = client.get(ETCD_CREDS_KEY)[0]
    return psycopg2.connect(host="localhost", database="usage_stats",
                            user="usage_stats", password=password.decode())


def vm_registrations(client):
    """{name: record} from /cluster/vms/ registrations."""
    vms = {}
    for value, metadata in client.get_prefix(VMS_PREFIX):
        if not value:
            continue
        try:
            name = metadata.key.decode()[len(VMS_PREFIX):]
            vms[name] = json.loads(value.decode())
        except Exception:
            continue
    return vms


def drain_events(client, conn):
    events = []
    for value, metadata in client.get_prefix(EVENTS_PREFIX):
        if not value:
            continue
        try:
            events.append((metadata.key.decode(), json.loads(value.decode())))
        except Exception:
            # Unparseable event: drop it rather than wedge the queue.
            client.delete(metadata.key.decode())
    if not events:
        return 0

    with conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO vm_events
                    (ts, vm, host, event, owner, gpus, initiator, billable, etcd_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (etcd_key) DO NOTHING
            """, [(e.get("ts"), e.get("vm"), e.get("host"), e.get("event"),
                   e.get("owner"), e.get("gpus"), e.get("initiator"),
                   bool(e.get("billable")), key) for key, e in events])
    for key, _ in events:
        client.delete(key)
    return len(events)


def ingest_snapshots(client, conn):
    vms = vm_registrations(client)
    now = datetime.now(timezone.utc)
    rows = []
    for value, metadata in client.get_prefix(VM_STATE_PREFIX):
        if not value:
            continue
        host = metadata.key.decode()[len(VM_STATE_PREFIX):]
        try:
            snap = json.loads(value.decode())
            ts = datetime.fromisoformat(snap["ts"])
        except Exception as e:
            print(f"Warning: bad snapshot from {host}: {e}")
            continue
        if now - ts > SNAPSHOT_MAX_AGE:
            continue
        for inst in snap.get("instances", []):
            rec = vms.get(inst.get("name"), {})
            rows.append((ts, inst.get("name"), host, rec.get("owner"),
                         inst.get("gpus", 0), inst.get("status"),
                         snap.get("interval_s", 120)))

    if rows:
        with conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO vm_samples
                        (ts, vm, host, owner, gpus, state, interval_s)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (vm, host, ts) DO NOTHING
                """, rows)
    return len(rows)


def main():
    if not is_storage_leader():
        return
    client = get_etcd_client()
    conn = get_db(client)
    try:
        drained = drain_events(client, conn)
        sampled = ingest_snapshots(client, conn)
        if drained or sampled:
            print(f"drained {drained} event(s), ingested {sampled} sample(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
