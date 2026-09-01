"""Pure helper + CLI for offsite backup retention.

Offsite destinations store whole-file ``.age`` backups (one per engine per
day) that are immutable — rotation is therefore only ever *deleting*. Rather
than keep every daily backup for some fixed window (which grows linearly and
fills a small offsite disk), this applies a dense-then-sparse policy per
engine:

* newest ``DAILY_DAYS`` days: keep every backup
* the next ``WEEKLY_DAYS`` days: keep at most one backup per ISO week
* anything older: keep at most one backup per calendar month

The decision is pure and side-effect-free (no etcd, no sockets) so it is
unit-testable exactly like :mod:`ycluster.utils.backup_status`. The caller
(generally ``backup-databases rotate``) gathers the remote file listing, pipes
it in, and executes the deletions.

Input (stdin), one line per backup file::

    <mtime-epoch>\\t<size-bytes>\\t<path>

``path`` is relative to the backup root (e.g. ``./etcd/20260821_002520.db.age``).
The backup *date* is taken from the leading 8 digits of the filename
(``YYYYMMDD_...``), which is stable regardless of rsync-touched mtimes; files
with no parseable date are never deleted.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

DEFAULT_DAILY_DAYS = 7
DEFAULT_WEEKLY_DAYS = 180


def parse_backup_date(name: str) -> _dt.date | None:
    """Return the backup's date from the leading 8 digits of the basename,
    or None if it can't be parsed."""
    base = name.rsplit("/", 1)[-1]
    digits = ""
    for ch in base:
        if not ch.isdigit():
            break
        digits += ch
    if len(digits) < 8:
        return None
    try:
        return _dt.date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def _engine_of(path: str) -> str:
    """Engine directory name from a relative backup path (``./etcd/x.age`` -> ``etcd``)."""
    parts = [p for p in path.split("/") if p not in ("", ".")]
    return parts[0] if parts else ""


def group_by_engine(entries: list) -> dict[str, list]:
    """Split a flat list of entries into per-engine lists (order preserved)."""
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e["engine"], []).append(e)
    return groups


def schedule_keep(files: list, today: _dt.date,
                  daily_days: int = DEFAULT_DAILY_DAYS,
                  weekly_days: int = DEFAULT_WEEKLY_DAYS) -> set:
    """Paths to keep under the dense-then-sparse schedule for one engine.

    `files` are entries (dicts with ``path`` and ``date``). Dated files are
    considered in newest-first order; undated files are never scheduled for
    deletion. Returns the set of paths to KEEP (everything else with a date is
    scheduled for deletion)."""
    dated = [f for f in files if f["date"] is not None]
    dated.sort(key=lambda f: f["date"], reverse=True)
    keep = {f["path"] for f in files if f["date"] is None}  # undated: always keep

    keep_buckets: set = set()
    for i, f in enumerate(dated):
        if i < daily_days:
            keep.add(f["path"])
            continue
        age_days = (today - f["date"]).days
        if age_days < weekly_days:
            iso = f["date"].isocalendar()
            bucket = (iso[0], iso[1])
        else:
            bucket = (f["date"].year, f["date"].month)
        if bucket not in keep_buckets:  # newest in bucket (list is newest-first)
            keep_buckets.add(bucket)
            keep.add(f["path"])
    return keep


def plan(entries: list, *, today: _dt.date | None = None,
         daily_days: int = DEFAULT_DAILY_DAYS,
         weekly_days: int = DEFAULT_WEEKLY_DAYS) -> dict:
    """Full rotation plan.

    `entries`: list of dicts with ``path``, ``size``, ``mtime``, ``date``.
    Returns a dict with the ``delete`` path list and simple accounting."""
    today = today or _dt.date.today()
    groups = group_by_engine(entries)
    keep: set = set()
    delete: list = []
    for _engine, files in groups.items():
        k = schedule_keep(files, today, daily_days, weekly_days)
        keep |= k
        for f in files:
            if f["path"] not in k:
                delete.append(f["path"])

    delete_bytes = sum(int(f.get("size") or 0) for f in entries
                       if f["path"] in delete)
    return {
        "delete": delete,
        "delete_bytes": delete_bytes,
        "delete_mb": round(delete_bytes / 1e6, 3),
        "kept": len(keep),
        "kept_bytes": sum(int(f.get("size") or 0) for f in entries if f["path"] in keep),
    }


def _parse_line(line: str) -> dict | None:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 3:
        return None
    mtime_s, size_s, path = parts[0], parts[1], "\t".join(parts[2:])
    try:
        return {
            "path": path,
            "date": parse_backup_date(path),
            "mtime": float(mtime_s),
            "size": int(float(size_s)),
            "engine": _engine_of(path),
        }
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-days", type=int, default=DEFAULT_DAILY_DAYS,
                        help="days of every-daily retention to keep")
    parser.add_argument("--weekly-days", type=int, default=DEFAULT_WEEKLY_DAYS,
                        help="days after DAILY_DAYS to keep one per ISO week")
    args = parser.parse_args(argv)

    entries = []
    for line in sys.stdin:
        e = _parse_line(line)
        if e is not None:
            entries.append(e)
    result = plan(entries, daily_days=args.daily_days, weekly_days=args.weekly_days)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
