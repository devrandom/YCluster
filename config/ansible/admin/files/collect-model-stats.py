#!/usr/bin/env python3
"""
Collect local-ai-proxy request logs and aggregate into usage_stats.model_usage.

Runs every 30 seconds via systemd timer. On each run:
1. Exits early if this node is not the storage leader (mountpoint check)
2. Reads last_processed_log_time from etcd (or defaults to 5 min ago)
3. Fetches journal logs since that timestamp
4. Parses request log lines, aggregates into 5-min buckets
5. Upserts into usage_stats.model_usage table
6. Writes latest log timestamp back to etcd
"""

import datetime
import json
import os
import subprocess
import sys
import time

import psycopg2
from ycluster.common.etcd_utils import get_etcd_client


ETCD_KEY = "/cluster/stats/model_usage/last_processed_log_time"
ETCD_CREDS_KEY = "/cluster/config/usage_stats/db-password"
JOURNAL_SINCE_DELTA = datetime.timedelta(minutes=5)
BUCKET_MINUTES = 5


def is_storage_leader():
    return subprocess.call(["mountpoint", "-q", "/rbd/user"]) == 0


def get_last_log_time(client):
    try:
        result = client.get(ETCD_KEY)
        if result[0] is not None:
            return float(result[0])
    except Exception:
        pass
    return None


def set_last_log_time(client, ts):
    client.put(ETCD_KEY, str(ts))


def floor_bucket(ts: float) -> float:
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    floored = dt.replace(
        minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
        second=0,
        microsecond=0,
    )
    return floored.timestamp()


def fetch_journal_logs(since_ts: float):
    since_dt = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cmd = [
        "journalctl",
        "-u", "local-ai-proxy",
        "-S", since_dt,
        "--output=json",
        "--no-pager",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    entries = []
    for line in proc.stdout:
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("MESSAGE", "")
        if not msg:
            continue
        try:
            log = json.loads(msg)
        except json.JSONDecodeError:
            continue
        if log.get("msg") != "request":
            continue
        entries.append(log)
    proc.wait()
    return entries


def aggregate_logs(entries):
    buckets = {}
    for e in entries:
        user = e.get("user", "") or ""
        model = e.get("model", "") or ""
        duration_ms = e.get("duration_ms", 0) or 0
        bytes_out = e.get("bytes_out", 0) or 0
        ts_str = e.get("time", "")
        if not ts_str or not model:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except Exception:
            continue

        period_start = floor_bucket(ts)
        period_end = period_start + BUCKET_MINUTES * 60

        key = (period_start, period_end, user, model)
        if key not in buckets:
            buckets[key] = {"request_count": 0, "total_duration_ms": 0, "total_bytes_out": 0}
        buckets[key]["request_count"] += 1
        buckets[key]["total_duration_ms"] += duration_ms
        buckets[key]["total_bytes_out"] += bytes_out

    return list(buckets.items())


def upsert_to_db(buckets, password):
    conn = psycopg2.connect(
        host="localhost",
        database="usage_stats",
        user="usage_stats",
        password=password,
    )
    with conn:
        with conn.cursor() as cur:
            for (period_start, period_end, user, model), stats in buckets:
                cur.execute("""
                    INSERT INTO model_usage (period_start, period_end, user_id, model, request_count, total_duration_ms, total_bytes_out)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (period_start, period_end, user_id, model)
                    DO UPDATE SET
                        request_count = model_usage.request_count + EXCLUDED.request_count,
                        total_duration_ms = model_usage.total_duration_ms + EXCLUDED.total_duration_ms,
                        total_bytes_out = model_usage.total_bytes_out + EXCLUDED.total_bytes_out
                """, (
                    datetime.datetime.fromtimestamp(period_start, tz=datetime.timezone.utc),
                    datetime.datetime.fromtimestamp(period_end, tz=datetime.timezone.utc),
                    user, model,
                    stats["request_count"], stats["total_duration_ms"], stats["total_bytes_out"],
                ))
    conn.close()


def main():
    if not is_storage_leader():
        return

    client = get_etcd_client()
    last_log_time = get_last_log_time(client)

    if last_log_time is None:
        last_log_time = time.time() - JOURNAL_SINCE_DELTA.total_seconds()

    entries = fetch_journal_logs(last_log_time)
    if not entries:
        return

    buckets = aggregate_logs(entries)
    if buckets:
        password_bytes = client.get(ETCD_CREDS_KEY)[0]
        upsert_to_db(buckets, password_bytes.decode())

    latest_ts = max(
        datetime.datetime.fromisoformat(e.get("time", "").replace("Z", "+00:00")).timestamp()
        for e in entries
    )
    set_last_log_time(client, latest_ts)


if __name__ == "__main__":
    main()