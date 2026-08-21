#!/usr/bin/env python3
"""Collect osv-feed CVE state into the node_exporter textfile directory.

Runs `osv-feed match --format prometheus` on the collector node and writes
the result (plus feed-health gauges) atomically to
/var/lib/prometheus/node-exporter/osv_feed.prom. stdlib-only; driven by a
short oneshot systemd timer, same discipline as node-temps-collector.

Why a single collector node: osv-feed's first-seen state
(<state>/first-seen.json) is maintained by the matching run, and the mirror
itself must have a single writer. Both live on one designated node, and the
collector only ever reads the published read-only snapshot on the shared
dir, so the writable DB never has concurrent writers.

Output:
  osv_feed_cve_info / osv_feed_cve_epss_score /
  osv_feed_cve_epss_percentile / osv_feed_cve_first_seen_seconds /
  osv_feed_cve_summary
    passthrough from `osv-feed --db-immutable match --format prometheus`
  osv_feed_mirror_present    1 when any mirrored ecosystem/EPSS cursor exists
  osv_feed_mirror_age_seconds  age of the most-stale mirrored feed cursor
  osv_feed_collector_error   1 when the last match run failed

The `.prom` file is always rewritten (even on failure it carries the health
gauges) so node_textfile_mtime_seconds advances and staleness alerts key
only on the script really stopping. When a match run fails the CVE series
are dropped rather than left showing a stale "no findings" state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

OUT = os.environ.get("OSV_FEED_OUT", "/var/lib/prometheus/node-exporter/osv_feed.prom")
OSV_FEED = os.environ.get("OSV_FEED_BIN", "/usr/local/bin/osv-feed")
DB_DIR = os.environ.get("OSV_FEED_DB_DIR", "/near1/osv-feed")
# Cursor files the mirror writes; the mirror is daily, so more than two
# missed days means the feed is stale and matching can't be trusted.
CURSOR_FILES = (
    "Ubuntu.cursor",
    "Debian.cursor",
    "Go.cursor",
    "PyPI.cursor",
    "epss.cursor",
)
# Directory of container/*.manifest files to match (see osv-feed's
# `match --container-dir`); empty/absent disables container matching.
CONTAINER_DIR = os.environ.get("OSV_FEED_CONTAINER_DIR", "")
MATCH_TIMEOUT = 600


def mirror_health(db_dir: str) -> tuple[bool, int]:
    """Return (present, age_seconds) for the most-stale feed cursor.

    Any cursor older than the others wins, so a single stuck ecosystem or
    EPSS mirror makes the whole feed look stale and trips the mirror alert.
    """
    now = time.time()
    present = False
    oldest_age = 0
    for name in CURSOR_FILES:
        path = os.path.join(db_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        present = True
        age = now - mtime
        if age > oldest_age:
            oldest_age = age
    return present, oldest_age


def run_match() -> tuple[bool, str, list[str]]:
    """Run osv-feed match, returning (ok, body, error_lines).

    When ``OSV_FEED_CONTAINER_DIR`` is set and non-empty, pass
    ``--container-dir`` so container image CVE findings (category=container,
    image label) are included alongside the host categories.
    """
    argv = [OSV_FEED, "--db-immutable", "match", "--format", "prometheus"]
    if CONTAINER_DIR.strip():
        argv += ["--container-dir", CONTAINER_DIR]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=MATCH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", [str(exc)]
    ok = proc.returncode == 0
    errors = [line for line in (proc.stderr or "").splitlines() if line.strip()]
    return ok, (proc.stdout if ok else ""), errors


def write_prom(present: bool, age: int, ok: bool, body: str, errors: list[str]) -> None:
    lines = []
    if body:
        lines.append(body.rstrip("\n"))
    lines.append("# HELP osv_feed_mirror_present 1 when a mirrored OSV ecosystem/EPSS feed is present.")
    lines.append("# TYPE osv_feed_mirror_present gauge")
    lines.append(f"osv_feed_mirror_present {1 if present else 0}")
    lines.append("# HELP osv_feed_mirror_age_seconds Age of the most-stale mirrored OSV feed cursor, in seconds.")
    lines.append("# TYPE osv_feed_mirror_age_seconds gauge")
    lines.append(f"osv_feed_mirror_age_seconds {int(age)}")
    lines.append("# HELP osv_feed_collector_error 1 when the last osv-feed match run failed.")
    lines.append("# TYPE osv_feed_collector_error gauge")
    lines.append(f"osv_feed_collector_error {0 if ok else 1}")
    if not ok and errors:
        lines.append("# osv-feed match failed: " + " | ".join(errors).replace("\n", " "))

    d = os.path.dirname(OUT)
    fd, tmp = tempfile.mkstemp(prefix=".osv_feed.", suffix=".prom", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.rename(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    present, age = mirror_health(DB_DIR)
    ok, body, errors = run_match()
    write_prom(present, age, ok, body, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
