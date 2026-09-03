#!/usr/bin/env python3
"""Emit apt freshness and lock metrics for node_exporter.

Reads the stamps maintained by apt-daily and probes the apt/dpkg lock files
without modifying them.  The script is intentionally dependency-free so it can
run from a systemd oneshot unit on every Linux node.
"""

import fcntl
import os
import tempfile
import time
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get(
        "APT_METRICS_DIR", "/var/lib/prometheus/node-exporter"
    )
)
OUT = Path(os.environ.get("APT_METRICS_OUT", TEXTFILE_DIR / "apt_status.prom"))

STAMPS = {
    "update": Path("/var/lib/apt/periodic/update-success-stamp"),
    "upgrade": Path("/var/lib/apt/periodic/upgrade-stamp"),
}

LOCKS = {
    "lists": Path("/var/lib/apt/lists/lock"),
    "dpkg": Path("/var/lib/dpkg/lock"),
    "dpkg_frontend": Path("/var/lib/dpkg/lock-frontend"),
}


def stamp_age_seconds(path: Path, now: float) -> float:
    """Return the age of an apt stamp, or now when the stamp is missing."""
    try:
        return max(0, now - path.stat().st_mtime)
    except FileNotFoundError:
        return now
    except OSError:
        # A permission or I/O error is a real problem: make the alert fire
        # rather than pretending that apt is current.
        return now


def lock_held(path: Path) -> int:
    """Return 1 when another process holds an apt/dpkg lock."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return 0
    except OSError:
        return 1

    try:
        # The file is opened read-only, so probe with a POSIX shared lock. It
        # conflicts with apt/dpkg's exclusive lock, but not with the read-only
        # descriptor itself.
        fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return 0
    except BlockingIOError:
        return 1
    finally:
        os.close(fd)


def render(now: float) -> str:
    lines = [
        "# HELP ycluster_apt_update_age_seconds Seconds since the last successful apt cache update",
        "# TYPE ycluster_apt_update_age_seconds gauge",
        f"ycluster_apt_update_age_seconds {stamp_age_seconds(STAMPS['update'], now):.0f}",
        "# HELP ycluster_apt_upgrade_age_seconds Seconds since the last apt upgrade pass",
        "# TYPE ycluster_apt_upgrade_age_seconds gauge",
        f"ycluster_apt_upgrade_age_seconds {stamp_age_seconds(STAMPS['upgrade'], now):.0f}",
        "# HELP ycluster_apt_lock_held Whether an apt or dpkg lock is currently held",
        "# TYPE ycluster_apt_lock_held gauge",
    ]
    for name, path in LOCKS.items():
        lines.append(f'ycluster_apt_lock_held{{lock="{name}"}} {lock_held(path)}')
    return "\n".join(lines) + "\n"


def write_output() -> None:
    now = time.time()
    text = render(now)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".apt_status.", dir=OUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, OUT)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    write_output()
