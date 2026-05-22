#!/usr/bin/env python3
"""Write ZFS pool health metrics for the node-exporter textfile collector.

Produces:
  ycluster_zpool_state{pool,health}              numeric state (0=ONLINE, ...)
  ycluster_zpool_capacity_ratio{pool}            used / total
  ycluster_zpool_fragmentation_ratio{pool}
  ycluster_zpool_errors_total{pool,type}         read/write/cksum errors summed across vdevs
  ycluster_zpool_data_errors{pool}               count from "errors:" line
  ycluster_zpool_last_scrub_timestamp_seconds{pool}
  ycluster_zpool_scrub_in_progress{pool}
"""

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

OUT = "/var/lib/prometheus/node-exporter/zfs_pool.prom"

STATE_NUM = {
    "ONLINE": 0,
    "DEGRADED": 1,
    "FAULTED": 2,
    "OFFLINE": 3,
    "REMOVED": 4,
    "UNAVAIL": 5,
    "SUSPENDED": 6,
}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def parse_pct(v):
    if v in ("-", ""):
        return 0.0
    return float(v.rstrip("%")) / 100.0


def parse_pool_errors(status_text, pool):
    """Return the pool-level (READ, WRITE, CKSUM) counts from `zpool status -p`.

    The pool row is the first non-header config row whose name matches the pool;
    ZFS aggregates child vdev errors into the pool row, so this is sufficient.
    """
    in_config = False
    for line in status_text.splitlines():
        s = line.strip()
        if s.startswith("config:"):
            in_config = True
            continue
        if s.startswith("errors:"):
            break
        if not in_config or not s:
            continue
        parts = s.split()
        if len(parts) < 5 or parts[0] != pool:
            continue
        try:
            return int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            return 0, 0, 0
    return 0, 0, 0


def parse_data_errors(status_text):
    """Return integer count from the 'errors:' line. 'No known data errors' -> 0."""
    m = re.search(r"^errors:\s*(.+)$", status_text, re.MULTILINE)
    if not m:
        return 0
    val = m.group(1).strip()
    if "No known data errors" in val:
        return 0
    n = re.search(r"(\d+)\s+data error", val)
    return int(n.group(1)) if n else 0


def parse_scrub(status_text):
    """Return (last_ts, in_progress). last_ts=0 if never scrubbed."""
    in_progress = 0
    last_ts = 0
    for line in status_text.splitlines():
        s = line.strip()
        if s.startswith("scan:"):
            if "scrub in progress" in s:
                in_progress = 1
            m = re.search(r"scrub repaired .* on (.+)$", s)
            if m:
                try:
                    last_ts = int(
                        datetime.strptime(m.group(1).strip(), "%a %b %d %H:%M:%S %Y").timestamp()
                    )
                except ValueError:
                    last_ts = 0
    return last_ts, in_progress


def main():
    try:
        listing = run(["zpool", "list", "-H", "-o", "name,health,capacity,fragmentation"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        # No zpool / no pools — write empty file so old metrics get cleared.
        listing = ""

    lines = [
        "# HELP ycluster_zpool_state Pool health state (0=ONLINE,1=DEGRADED,2=FAULTED,3=OFFLINE,4=REMOVED,5=UNAVAIL,6=SUSPENDED,99=unknown)",
        "# TYPE ycluster_zpool_state gauge",
        "# HELP ycluster_zpool_capacity_ratio Pool used capacity as ratio of total",
        "# TYPE ycluster_zpool_capacity_ratio gauge",
        "# HELP ycluster_zpool_fragmentation_ratio Pool fragmentation as ratio",
        "# TYPE ycluster_zpool_fragmentation_ratio gauge",
        "# HELP ycluster_zpool_errors_total Device read/write/cksum errors summed across vdevs",
        "# TYPE ycluster_zpool_errors_total gauge",
        "# HELP ycluster_zpool_data_errors Pool-level data error count from 'zpool status'",
        "# TYPE ycluster_zpool_data_errors gauge",
        "# HELP ycluster_zpool_last_scrub_timestamp_seconds Unix timestamp of last completed scrub",
        "# TYPE ycluster_zpool_last_scrub_timestamp_seconds gauge",
        "# HELP ycluster_zpool_scrub_in_progress 1 if a scrub is currently running",
        "# TYPE ycluster_zpool_scrub_in_progress gauge",
    ]

    for row in listing.splitlines():
        if not row.strip():
            continue
        name, health, cap, frag = row.split("\t")
        state = STATE_NUM.get(health, 99)
        status = run(["zpool", "status", "-p", name])
        r, w, c = parse_pool_errors(status, name)
        data_errs = parse_data_errors(status)
        last_scrub, in_progress = parse_scrub(status)

        lines += [
            f'ycluster_zpool_state{{pool="{name}",health="{health}"}} {state}',
            f'ycluster_zpool_capacity_ratio{{pool="{name}"}} {parse_pct(cap):.4f}',
            f'ycluster_zpool_fragmentation_ratio{{pool="{name}"}} {parse_pct(frag):.4f}',
            f'ycluster_zpool_errors_total{{pool="{name}",type="read"}} {r}',
            f'ycluster_zpool_errors_total{{pool="{name}",type="write"}} {w}',
            f'ycluster_zpool_errors_total{{pool="{name}",type="cksum"}} {c}',
            f'ycluster_zpool_data_errors{{pool="{name}"}} {data_errs}',
            f'ycluster_zpool_last_scrub_timestamp_seconds{{pool="{name}"}} {last_scrub}',
            f'ycluster_zpool_scrub_in_progress{{pool="{name}"}} {in_progress}',
        ]

    out_dir = os.path.dirname(OUT)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".zfs_pool.prom.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
