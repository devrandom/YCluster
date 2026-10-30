#!/usr/bin/env python3
"""Emit apt freshness, lock and pending-restart metrics for node_exporter.

Reads the stamps maintained by apt-daily, probes the apt/dpkg lock files
without modifying them, and asks needrestart which services are still running
against replaced libraries.  The script is intentionally dependency-free so it
can run from a systemd oneshot unit on every Linux node.
"""

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get(
        "APT_METRICS_DIR", "/var/lib/prometheus/node-exporter"
    )
)
OUT = Path(os.environ.get("APT_METRICS_OUT", TEXTFILE_DIR / "apt_status.prom"))

UU_LOG = Path(
    os.environ.get(
        "APT_UU_LOG", "/var/log/unattended-upgrades/unattended-upgrades.log"
    )
)

# unattended-upgrades runs without a tty, so a package whose local conffile
# differs from the shipped one is skipped on every run ("has conffile
# prompt and needs to be upgraded manually"). That is silent by design
# upstream, so the last run's log is the semantic source for this metric.
UU_RUN_START = "Starting unattended upgrades script"
UU_CONFFILE_PROMPT = re.compile(r"WARNING Package (\S+) has conffile prompt")

STAMPS = {
    "update": Path("/var/lib/apt/periodic/update-success-stamp"),
    "upgrade": Path("/var/lib/apt/periodic/upgrade-stamp"),
}

LOCKS = {
    "lists": Path("/var/lib/apt/lists/lock"),
    "dpkg": Path("/var/lib/dpkg/lock"),
    "dpkg_frontend": Path("/var/lib/dpkg/lock-frontend"),
}

# needrestart greylists these itself (restarting them is user-visible or
# meaningless), so a stale library there is not actionable on its own.
RESTART_IGNORED = frozenset(
    {
        "dbus.service",
        "systemd-logind.service",
        "networkd-dispatcher.service",
        "unattended-upgrades.service",
    }
)

# Per-session units that needrestart never restarts itself.
RESTART_IGNORED_PREFIXES = ("getty@", "serial-getty@", "user@")

NEEDRESTART = shutil.which("needrestart") or "/usr/sbin/needrestart"

# Last apt-get update output, written by the apt-cache-update unit.
APT_UPDATE_LOG = Path("/run/ycluster/apt-update.log")
APT_FAILURE_RE = re.compile(r"^(?:W: Failed to fetch|Err:?\d*)\s+(\S+)")


def actionable_service(name: str) -> bool:
    """Return False for units whose stale libraries are not actionable."""
    if name in RESTART_IGNORED:
        return False
    return not name.startswith(RESTART_IGNORED_PREFIXES)


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


def parse_needrestart(output: str) -> tuple[list[str], int]:
    """Parse `needrestart -b` output into (services, kernel upgrade pending).

    Batch output lists every service whose binaries or libraries were
    replaced, including the ones needrestart would refuse to restart, so the
    greylisted units are filtered out here.  The kernel status is one of
    NRK_UNKNOWN/NRK_NOUPGRADE (0/1, nothing to do) or NRK_ABIUPGRADE/
    NRK_VERUPGRADE (2/3, reboot pending).
    """
    services: list[str] = []
    kernel_pending = 0
    for line in output.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "NEEDRESTART-SVC":
            if value and actionable_service(value):
                services.append(value)
        elif key == "NEEDRESTART-KSTA":
            try:
                kernel_pending = 1 if int(value) >= 2 else 0
            except ValueError:
                kernel_pending = 0
    return services, kernel_pending


def restart_metrics() -> list[str]:
    """Return pending-restart metrics, or nothing when needrestart is absent."""
    if not os.access(NEEDRESTART, os.X_OK):
        return []
    try:
        result = subprocess.run(
            [NEEDRESTART, "-b"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    services, kernel_pending = parse_needrestart(result.stdout)
    lines = [
        "# HELP ycluster_restart_service_pending Services running replaced libraries that need a restart",
        "# TYPE ycluster_restart_service_pending gauge",
    ]
    for name in sorted(services):
        lines.append(f'ycluster_restart_service_pending{{service="{name}"}} 1')
    lines.extend(
        [
            "# HELP ycluster_restart_kernel_pending Whether an installed kernel supersedes the running one",
            "# TYPE ycluster_restart_kernel_pending gauge",
            f"ycluster_restart_kernel_pending {kernel_pending}",
        ]
    )
    return lines


def parse_conffile_prompts(log_text: str) -> list[str]:
    """Return packages the most recent unattended-upgrades run skipped.

    Only the last run is considered: a package that was upgraded or
    reconciled since then must stop alerting instead of lingering in an
    older log line.
    """
    skipped: list[str] = []
    for line in log_text.splitlines():
        if UU_RUN_START in line:
            skipped = []
            continue
        match = UU_CONFFILE_PROMPT.search(line)
        if match:
            skipped.append(match.group(1))
    return sorted(set(skipped))


def conffile_prompt_metrics() -> list[str]:
    """Return per-package metrics for conffile prompts in the last run."""
    try:
        log_text = UU_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # No log means no evidence of a prompt (fresh install or rotation);
        # emit the empty family rather than inventing a series.
        return []
    lines = [
        "# HELP ycluster_apt_conffile_prompt Package skipped by the last unattended-upgrades run over a conffile prompt",
        "# TYPE ycluster_apt_conffile_prompt gauge",
    ]
    for package in parse_conffile_prompts(log_text):
        lines.append(f'ycluster_apt_conffile_prompt{{package="{package}"}} 1')
    return lines


def parse_apt_update_failures(output: str) -> list[str]:
    """Return the source URIs apt failed to fetch in one update run.

    apt-get update exits 0 when a single repository is unreachable, so the
    success stamp cannot distinguish a broken mirror from a quiet day; the
    failure lines are the only record of it.
    """
    sources: list[str] = []
    for line in output.splitlines():
        match = APT_FAILURE_RE.match(line.strip())
        if match:
            # Err: and "Failed to fetch" name the same repository at
            # different depths; key on the base URI so they collapse.
            sources.append(match.group(1).split("/dists/")[0])
    return list(dict.fromkeys(sources))


def fetch_failure_metrics() -> list[str]:
    """Return per-source fetch-failure metrics from the last update run."""
    try:
        output = APT_UPDATE_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = [
        "# HELP ycluster_apt_fetch_failure apt-get update could not fetch this source in the last run",
        "# TYPE ycluster_apt_fetch_failure gauge",
    ]
    for source in sorted(set(parse_apt_update_failures(output))):
        lines.append(f'ycluster_apt_fetch_failure{{source="{source}"}} 1')
    return lines


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
    lines.extend(conffile_prompt_metrics())
    lines.extend(restart_metrics())
    lines.extend(fetch_failure_metrics())
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
