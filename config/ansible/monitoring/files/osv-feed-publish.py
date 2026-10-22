#!/usr/bin/env python3
"""Publish a read-only osv-feed snapshot to a shared directory.

Copies the writable local store's artifacts that a read-only `match` needs
(the SQLite store, the EPSS snapshot, and the per-ecosystem cursors) to a
shared directory such as /near1, using atomic tmp+rename so consumers that
open with `--db-immutable` always see a complete file. Runs after each
daily mirror.

`first-seen.json` is intentionally NOT published: it is per-consumer
writable state maintained by the matching run via --state-dir
(OSV_FEED_STATE_DIR) and must stay on a local writable disk.

Source and destination directories come from the environment so the same
script is reused by the systemd mirror/publish job and can be run by hand:
  OSV_FEED_DB_DIR        writable local DB dir being mirrored
  OSV_FEED_SNAPSHOT_DIR  shared dir to publish the snapshot into
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

SRC = os.environ.get("OSV_FEED_DB_DIR", "/var/lib/osv-feed/db")
DEST = os.environ.get("OSV_FEED_SNAPSHOT_DIR", "/near1/osv-feed")
# Everything a read-only `match` needs, including the cursors it consults to
# decide whether the feed is fresh. first-seen.json is deliberately absent.
FILES = (
    "osv-feed.sqlite",
    "epss.json",
    "Ubuntu.cursor",
    "Debian.cursor",
    "Go.cursor",
    "PyPI.cursor",
    "Alpine.cursor",
    "epss.cursor",
)


def atomic_copy(src: str, dst_dir: str) -> None:
    os.makedirs(dst_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".osv-snap-", suffix=".tmp", dir=dst_dir)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(dst_dir, os.path.basename(src)))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    for name in FILES:
        src = os.path.join(SRC, name)
        if not os.path.isfile(src):
            print(f"osv-feed-publish: skip {name}: not present", file=sys.stderr)
            continue
        atomic_copy(src, DEST)
        print(f"osv-feed-publish: published {name} -> {DEST}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
