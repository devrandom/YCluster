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
4. Closes intervals samples show are over: when a VM is observed stopped
   but its last lifecycle event still opened an interval (manual incus
   op, reboot without autostart, lost queue event), appends the stop the
   log missed at the last moment samples saw it running.

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


def reconcile_unclosed_stops(conn):
    """Append the stop event for intervals samples show are already over.

    A manual `incus stop`, a host reboot without autostart, or a lost
    queue event leaves a launch/start with no stop. Interval assembly
    would then accrue that "run" to now forever — and, once the VM is
    started again, carry the whole gap into the new run, because a start
    on an already-open interval is a no-op. Closing it here keeps the
    event log authoritative and makes the next start open a fresh
    interval.

    The stop is stamped at the last moment samples saw the VM running and
    only for VMs with a fresh sample: a dead sampler means unknown, not
    stopped. Idempotent through the UNIQUE etcd_key column, which also
    ignores a real stop that the drain already recorded.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (vm) vm, host, ts, owner, gpus, state
                    FROM vm_samples
                    WHERE ts > now() - interval '10 minutes'
                    ORDER BY vm, ts DESC
                ), last_run AS (
                    SELECT vm, MAX(ts) AS ts FROM vm_samples
                    WHERE state = 'Running' GROUP BY vm
                ), last_stop AS (
                    SELECT vm, MAX(ts) AS ts FROM vm_events
                    WHERE event IN ('stop', 'destroy') GROUP BY vm
                ), opener AS (
                    -- Start of the interval that is still open: the first
                    -- launch/start/restart after the last stop.
                    SELECT e.vm, MIN(e.ts) AS ts
                    FROM vm_events e
                    LEFT JOIN last_stop s ON s.vm = e.vm
                    WHERE e.event IN ('launch', 'start', 'restart')
                      AND (s.ts IS NULL OR e.ts > s.ts)
                    GROUP BY e.vm
                )
                SELECT l.vm, l.host, l.owner, l.gpus,
                       -- +1µs keeps a zero-length stop strictly after the
                       -- start that opened it: equal timestamps leave the
                       -- assembly's ORDER BY ts tie ambiguous and can
                       -- process the stop first, leaving it open.
                       GREATEST(o.ts + interval '1 microsecond',
                                COALESCE(r.ts, o.ts))
                FROM latest l
                JOIN opener o ON o.vm = l.vm
                LEFT JOIN last_run r ON r.vm = l.vm
                WHERE l.state <> 'Running'
            """)
            rows = [(stopped, vm, host, owner, gpus,
                     f'sample-stop:{vm}:{stopped.isoformat()}')
                    for vm, host, owner, gpus, stopped in cur.fetchall()]
            if rows:
                cur.executemany("""
                    INSERT INTO vm_events
                        (ts, vm, host, event, owner, gpus, initiator,
                         billable, etcd_key)
                    VALUES (%s, %s, %s, 'stop', %s, %s, 'samples',
                            FALSE, %s)
                    ON CONFLICT (etcd_key) DO NOTHING
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
        closed = reconcile_unclosed_stops(conn)
        if drained or sampled or closed:
            print(f"drained {drained} event(s), ingested {sampled} "
                  f"sample(s), closed {closed} unclosed interval(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
