"""Pure scheduling and GPU admission helpers for VMs."""

from datetime import datetime, timedelta, timezone


def _parse_window(w, now=None):
    """Parse one {start, end} window into a (start, end) pair of UTC
    datetimes, or None if malformed/naive/elapsed. `now` (if given)
    drops windows whose end is already past."""
    try:
        start = datetime.fromisoformat(str(w["start"]))
        end = datetime.fromisoformat(str(w["end"]))
    except (KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start or (now is not None and end <= now):
        return None
    return start, end

def desired_on(desired, now):
    """Evaluate a desired-state record at `now` (UTC).

    Windows are only consulted under 'schedule'; on/off/unmanaged keep
    their windows as dormant data (preserved across mode flips by the web
    page), so they must never be read as on/off intent. Anything that
    isn't 'on' or an active 'schedule' window is off."""
    mode = desired.get("mode")
    if mode == "on":
        return True
    if mode == "schedule":
        for w in desired.get("windows", []):
            parsed = _parse_window(w)
            if parsed and parsed[0] <= now < parsed[1]:
                return True
    return False

def pending_state(desired, status, grace, now):
    """What the reconciler will do to this VM on its next tick — 'start',
    'stop', or None (converged / unmanaged) — from its desired record vs
    its live `status`. Only managed modes converge. A `grace` marker is the
    reconciler's own (fresher) evidence of a running, pending-stop VM, used
    when the periodically-sampled `status` lags. The schedule page surfaces
    this as the ⏳ pending indicator."""
    if not desired or desired.get("mode") not in ("on", "off", "schedule"):
        return None
    want_on = desired_on(desired, now)
    live_on = status == "Running"
    if want_on and not live_on:
        return "start"
    if not want_on and (live_on or grace):
        return "stop"
    return None

# --------------------------------------------------------------------------
# GPU capacity commitments (admission control for the scheduling page)
#
# The dual of desired_on: rather than "is this VM on now", it answers
# "which GPUs are spoken for, when". A VM's registered GPUs are committed
# *always* under unmanaged (devices never released) and `on`, *never*
# under `off` (scheduled stop releases them), and during its windows
# under `schedule`. Containers share the host GPU and never enter pool
# accounting. Pure functions — no etcd, so unit-testable in isolation;
# the admission orchestration (etcd reads) stays in the admin app.
# --------------------------------------------------------------------------
def desired_gpus(desired, registered):
    """How many GPUs a VM should run with under a desired-state record.

    Uses the record's `gpus` when explicitly set (0 means run with no
    GPUs); falls back to the VM's registered allocation when absent
    (backwards compat — schedules written before this field meant 'all
    allocated GPUs')."""
    d = desired or {}
    if "gpus" in d:
        g = d["gpus"]
        if isinstance(g, int) and not isinstance(g, bool) \
                and 0 <= g <= registered:
            return g
    return registered

def gpu_commitments(registry, desired_all, now):
    """{host: [{vm, owner, gpus, mode, windows}]} implied by the registry
    and the accepted desired records. `windows` is None for always-held
    commitments, else a list of (start, end) UTC datetime pairs with
    elapsed windows dropped (a schedule with no live windows commits
    nothing and is omitted)."""
    hosts = {}
    for vm, rec in registry.items():
        if rec.get("type") == "container":
            continue
        host = rec.get("host")
        gpus = desired_gpus(desired_all.get(vm), rec.get("gpus") or 0)
        if not gpus or not host:
            continue
        mode = (desired_all.get(vm) or {}).get("mode", "unmanaged")
        if mode == "off":
            continue
        windows = None
        if mode == "schedule":
            windows = [p for p in
                       (_parse_window(w, now)
                        for w in (desired_all.get(vm) or {}).get("windows", []))
                       if p]
            if not windows:
                continue
        hosts.setdefault(host, []).append(
            {"vm": vm, "owner": rec.get("owner"), "gpus": gpus,
             "mode": mode, "windows": windows})
    return hosts

def gpu_block_commitments(blocks, now):
    """{host: [{vm, owner, gpus, mode, windows, is_block}]} implied by the
    admin GPU reservations `blocks` ({block_id: record}). Same shape as
    `gpu_commitments` (so `gpu_conflict` and the schedule page can treat
    blocks as ordinary held capacity) with `owner` always None, `vm` set
    to the block id, and `is_block` True to let the UI label them. A
    block's `mode` is either 'on' (held continuously) or 'schedule'
    (held during its live windows); an 'on'-mode block with no pool is
    merely dormant and holds nothing."""
    hosts = {}
    for block_id, b in blocks.items():
        gpus, host = b.get("gpus") or 0, b.get("host")
        if not gpus or not host:
            continue
        mode = b.get("mode", "on")
        windows = None
        if mode == "schedule":
            windows = [p for p in
                       (_parse_window(w, now) for w in b.get("windows", []))
                       if p]
            if not windows:
                continue
        hosts.setdefault(host, []).append(
            {"vm": block_id, "owner": None, "gpus": gpus, "mode": mode,
             "windows": windows, "is_block": True})
    return hosts

def combine_gpu_commitments(*commit_sets):
    """Merge any number of {host: [commit, ...]} dicts (from
    `gpu_commitments` / `gpu_block_commitments`) into one, preserving
    per-host order. Used so VM and block commitments are checked and shown
    together against a single pool."""
    merged = {}
    for cs in commit_sets:
        for host, items in cs.items():
            merged.setdefault(host, []).extend(items)
    return merged

def gpu_conflict(commits, gpus, pool, lo, hi):
    """Worst capacity violation for a candidate needing `gpus` GPUs
    throughout [lo, hi): (when, free_then, holders), or None if it fits.
    Committed capacity only changes at window starts, so probing `lo`
    plus every window start inside the range covers the peaks."""
    points = [lo]
    for c in commits:
        for s, _ in (c["windows"] or []):
            if lo < s < hi:
                points.append(s)
    worst = None
    for p in points:
        active = [c for c in commits
                  if c["windows"] is None
                  or any(s <= p < e for s, e in c["windows"])]
        free = pool - sum(c["gpus"] for c in active)
        if free < gpus and (worst is None or free < worst[1]):
            worst = (p, free, active)
    return worst

def gpu_available_windows(commits, gpus, pool, now, count=3, max_hours=6,
                          horizon_days=7, avoid_windows=None):
    """Future hourly-aligned windows where `gpus` GPUs fit `pool` against
    `commits`, for the schedule page's availability suggestions.

    Returns up to `count` windows, each starting at the next whole hour and
    lasting the longest continuous span that fits, capped at `max_hours`
    (6 by default, to discourage over-allocation). Suggested starts that
    overlap `avoid_windows` (a list of (start, end) UTC pairs) are skipped,
    so a VM's own already-scheduled windows don't get re-suggested.
    """
    if not isinstance(pool, int) or not gpus or gpus > pool:
        return []
    avoid_windows = avoid_windows or []
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    horizon = start + timedelta(days=horizon_days)
    out = []
    t = start
    while t < horizon and len(out) < count:
        end = None
        for hours in range(1, max_hours + 1):
            candidate = t + timedelta(hours=hours)
            if gpu_conflict(commits, gpus, pool, t, candidate) is None:
                end = candidate
        if end is not None and any(t < hi and lo < end
                                   for lo, hi in avoid_windows):
            t += timedelta(hours=1)
            continue
        if end is None:
            t += timedelta(hours=1)
            continue
        out.append({
            'start': t.isoformat(timespec='minutes'),
            'end': end.isoformat(timespec='minutes'),
            'hours': int((end - t).total_seconds() // 3600),
        })
        t = end
    return out
