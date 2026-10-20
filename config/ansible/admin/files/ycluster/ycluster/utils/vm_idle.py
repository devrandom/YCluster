"""Idle-GPU detection from per-VM GPU utilization.

GPU utilization comes from ``node_vm_gpu_utilization_percent``, which
vm-gpu-collector emits for every VFIO-passthrough GPU by running nvidia-smi
inside the VM. The series only exists while the VM is running, so its coverage
also proves the VM was up for the window. A VM counts as in use when either
threshold is met inside the lookback window:

  * more than 20% GPU utilization in at least 10 one-minute samples, or
  * more than 5% GPU utilization in at least 20 one-minute samples.

A VM with no GPUs, or with no per-VM GPU metric, never alerts. The series
must cover the whole window, up to the present, with no gap over 10 minutes:
a VM stopped an hour ago has a quiet history but nothing to remind its owner
about.
"""

from datetime import datetime, timedelta, timezone
import json
import math
import os
import sys

import requests

from ..common.etcd_utils import get_etcd_client
from . import vm_manager as vm

HIGH_PCT = 20.0
HIGH_SAMPLES = 10
LOW_PCT = 5.0
LOW_SAMPLES = 20
IDLE_AFTER = timedelta(hours=24)
# A gap longer than this means the VM was stopped or the collector could not
# read its GPU for part of the window, so idleness over the full day is unknown.
MAX_GAP = timedelta(minutes=10)
# Remind the owner about a VM that is still idle at most once a day. The timer
# runs hourly; this keeps a persistently idle VM to one DM per day.
REALERT_AFTER = timedelta(days=1)
ALERTS_PREFIX = "/cluster/vm-idle/"
# Owners listed here get no idle alerts at all. Deployment-specific (owner
# emails must not live in the public repo), so the list is kept in etcd and
# edited with `ycluster vm idle --mute-owner/--unmute-owner`.
SUPPRESS_KEY = "/cluster/config/vm-idle/suppress"
# The admin web UI's VM page, where owners stop their VMs ("Stop now").
VM_PAGE_PATH = "/admin/vm-schedule"

# The central Prometheus runs on the storage leader, like this check.
PROMETHEUS_URL = os.environ.get("YCLUSTER_PROMETHEUS_URL",
                                "http://admin.xc:9091")
GPU_METRIC = "node_vm_gpu_utilization_percent"
# One point per minute, the mean of the 30s scrapes in that minute. Max across
# GPUs and federated copies: a VM is busy when any of its GPUs is busy.
GPU_QUERY = f"max by (vm) (avg_over_time({GPU_METRIC}[1m]))"
ZULIP_DB = "zulip"
STORAGE_VIP = "10.0.0.100"
ZULIP_DB_PASSWORD_KEY = "/cluster/config/zulip/postgres_password"
ZULIP_BOT_EMAIL_KEY = "/cluster/config/zulip/vm-idle-bot-email"
ZULIP_BOT_API_KEY = "/cluster/config/zulip/vm-idle-bot-api-key"


def fetch_gpu_series(now=None, idle_after=IDLE_AFTER, step=60, url=None):
    """Per-VM GPU utilization over the window, keyed by VM name.

    Each value is a ``(timestamp, percent)`` pair at one-minute resolution.
    Raises when Prometheus is unreachable or returns an error: a monitoring
    outage must not be mistaken for "no VM is idle".
    """
    now = now or datetime.now(timezone.utc)
    try:
        r = requests.get(
            f"{url or PROMETHEUS_URL}/api/v1/query_range",
            params={"query": GPU_QUERY,
                     "start": int((now - idle_after).timestamp()),
                     "end": int(now.timestamp()),
                     "step": step},
            timeout=30)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"cannot query Prometheus: {exc}") from exc
    if payload.get("status") != "success":
        raise RuntimeError(
            f"Prometheus query failed: {payload.get('error', payload)}")

    out = {}
    for series in payload["data"]["result"]:
        name = series.get("metric", {}).get("vm")
        if not name:
            continue
        points = []
        for ts, value in series.get("values", []):
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points.append((datetime.fromtimestamp(float(ts), timezone.utc),
                                value))
        out[name] = points
    return out


def evaluate(points, now=None, idle_after=IDLE_AFTER, high_pct=HIGH_PCT,
             high_samples=HIGH_SAMPLES, low_pct=LOW_PCT,
             low_samples=LOW_SAMPLES):
    """Evaluate one VM's GPU points; returns counts, verdict and alert flag."""
    now = now or datetime.now(timezone.utc)
    window = sorted((ts, value) for ts, value in points
                    if ts >= now - idle_after)
    values = [value for _, value in window]
    high = sum(1 for v in values if v > high_pct)
    low = sum(1 for v in values if v > low_pct)
    active = high >= high_samples or low >= low_samples

    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(window, window[1:])]
    max_gap = max(gaps) if gaps else 0
    # Both ends matter: the series must start with the window (the VM was up
    # for the whole day) and still be live now. A VM stopped an hour ago has a
    # quiet history but nothing to alert its owner about.
    covered = bool(window) and (
        window[0][0] <= now - idle_after + MAX_GAP
        and max_gap <= MAX_GAP.total_seconds()
        and window[-1][0] >= now - MAX_GAP)

    if not window:
        status = "no GPU data"
    elif not covered:
        status = "insufficient data"
    elif active:
        status = "in use"
    else:
        status = "idle"

    return {
        "samples": len(window),
        "high_samples": high,
        "low_samples": low,
        "max_util": max(values) if values else None,
        "covered": covered,
        "active": active,
        "status": status,
        "alert": status == "idle",
    }


def report(now=None, idle_after=IDLE_AFTER):
    """Evaluate every registered VM; returns one result dict per VM."""
    now = now or datetime.now(timezone.utc)
    series = fetch_gpu_series(now=now, idle_after=idle_after)
    results = []
    for name, rec in sorted(vm.vms_all().items()):
        gpus = rec.get("gpus") or 0
        points = series.get(name, [])
        result = evaluate(points, now=now, idle_after=idle_after)
        if not gpus:
            result.update(status="no GPU", alert=False)
        elif not points:
            result.update(status="no GPU data", alert=False)
        result.update(name=name, owner=rec.get("owner"),
                       host=rec.get("host"), gpus=gpus)
        results.append(result)
    return results


def format_report(results):
    """Render the report as a fixed-width table."""
    lines = [
        f"{'VM':<16} {'OWNER':<28} {'STATUS':<18} {'SAMPLES':>7} "
        f"{'>20%':>5} {'>5%':>4} {'MAX':>7}",
    ]
    for r in sorted(results, key=lambda r: (not r["alert"], r["name"])):
        max_util = f"{r['max_util']:.0f}%" if r["max_util"] is not None else "-"
        lines.append(
            f"{r['name']:<16} {(r['owner'] or '-'):<28} {r['status']:<18} "
            f"{r['samples']:>7} {r['high_samples']:>5} "
            f"{r['low_samples']:>4} {max_util:>7}")
    return "\n".join(lines)


def _alert_key(name):
    return ALERTS_PREFIX + name


def _alert_due(name, now):
    """True when this idle episode has not been alerted yet or is due again."""
    rec = vm._get_json(_alert_key(name)) or {}
    sent = rec.get("alerted_at")
    if not sent:
        return True
    try:
        sent = datetime.fromisoformat(sent)
    except ValueError:
        return True
    return now - sent >= REALERT_AFTER


def _mark_alerted(name, now):
    rec = vm._get_json(_alert_key(name)) or {}
    rec["alerted_at"] = now.isoformat(timespec="seconds")
    vm._put_json(_alert_key(name), rec)


def clear_alert(name):
    if vm._get_json(_alert_key(name)):
        vm._delete(_alert_key(name))


def suppressed_owners():
    """Owners who have opted out of idle alerts, lowercased."""
    raw, _ = get_etcd_client().get(SUPPRESS_KEY)
    if not raw:
        return set()
    try:
        values = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(values, list):
        return set()
    return {str(v).strip().lower() for v in values if str(v).strip()}


def set_suppressed(owner, suppressed):
    """Add or remove an owner; returns the resulting sorted list."""
    owners = suppressed_owners()
    key = owner.strip().lower()
    if not key:
        raise ValueError("owner email is required")
    if suppressed:
        owners.add(key)
    else:
        owners.discard(key)
    ordered = sorted(owners)
    get_etcd_client().put(SUPPRESS_KEY, json.dumps(ordered))
    return ordered


def _zulip_settings():
    client = get_etcd_client()
    email, _ = client.get(ZULIP_BOT_EMAIL_KEY)
    api_key, _ = client.get(ZULIP_BOT_API_KEY)
    domain, _ = client.get("/cluster/https/domain")
    if not (email and api_key and domain):
        return None
    return email.decode(), api_key.decode(), domain.decode()


def _message(result, idle_after, vm_page=None):
    hours = int(idle_after.total_seconds() // 3600)
    stop = (f"If you are done with it, open {vm_page} and press **Stop now** "
            f"on the VM." if vm_page else
            "If you are done with it, stop it from the admin web UI.")
    return (
        f"**{result['name']}** has been running with no meaningful GPU "
        f"activity for the last {hours}h.\n"
        f"Owner: {result['owner']}\n"
        f"One-minute samples: {result['samples']}, "
        f">{HIGH_PCT:.0f}%: {result['high_samples']}, "
        f">{LOW_PCT:.0f}%: {result['low_samples']}.\n"
        f"{stop}")


def resolve_recipient(owner):
    """The address the Zulip API accepts for a VM owner.

    Realms that hide email addresses (`EMAIL_ADDRESS_VISIBILITY_ADMINS`)
    expose a realm-local address such as `user7@zulip.example` to the API,
    while VM registrations carry the owner's real login address. The Zulip
    user table maps the two; without that map the DM is rejected as an
    invalid email even though the person exists.
    """
    if not owner:
        return owner
    password, _ = get_etcd_client().get(ZULIP_DB_PASSWORD_KEY)
    if not password:
        return owner
    import psycopg2   # lazy: only alerting needs the database
    try:
        conn = psycopg2.connect(host=STORAGE_VIP, database=ZULIP_DB,
                                user="zulip", password=password.decode())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email FROM zerver_userprofile "
                    "WHERE lower(delivery_email) = lower(%s) "
                    "AND is_active AND NOT is_bot "
                    "ORDER BY id LIMIT 1", (owner,))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:   # noqa: BLE001 - alerting must not die on lookup
        print(f"Cannot resolve Zulip address for {owner}: {exc}",
              file=sys.stderr, flush=True)
        return owner
    return row[0] if row else owner


def send_zulip(settings, result, idle_after=IDLE_AFTER, stream=None):
    """Send the alert as a DM to the owner, or to `stream` when DMs fail.

    Returns 'direct', 'stream' or 'failed'. DMs fail when the owner has no
    Zulip account in this realm; the stream post still carries the owner.
    """
    email, api_key, domain = settings
    url = f"https://zulip.{domain}/api/v1/messages"
    content = _message(result, idle_after,
                       vm_page=f"https://admin.{domain}{VM_PAGE_PATH}")
    # Zulip's message endpoint parses form-encoded parameters; posting JSON
    # gets rejected with "Missing 'content' argument". `to` is a JSON list in
    # either encoding.
    payload = {"type": "direct",
               "to": json.dumps([resolve_recipient(result["owner"])]),
               "content": content}
    try:
        r = requests.post(url, auth=(email, api_key), data=payload, timeout=15)
        if r.ok:
            return "direct"
        if not stream:
            return "failed"
    except requests.RequestException:
        if not stream:
            return "failed"
    payload = {"type": "stream", "to": stream, "topic": "idle VMs",
               "content": content}
    try:
        r = requests.post(url, auth=(email, api_key), data=payload, timeout=15)
        return "stream" if r.ok else "failed"
    except requests.RequestException:
        return "failed"


def notify(results, now=None, idle_after=IDLE_AFTER, stream=None):
    """Alert once per idle episode; clear markers when a VM stops or gets busy."""
    now = now or datetime.now(timezone.utc)
    settings = _zulip_settings()
    if settings is None:
        raise RuntimeError(
            "Zulip bot credentials are missing: set "
            f"{ZULIP_BOT_EMAIL_KEY} and {ZULIP_BOT_API_KEY}")
    sent = 0
    muted = suppressed_owners()
    for result in results:
        # A running VM with a collector gap keeps its marker: a gap is not
        # evidence of use, so the episode continues rather than re-alerting.
        if result["status"] in ("in use", "no GPU", "no GPU data"):
            clear_alert(result["name"])
        if not result["alert"]:
            continue
        if (result["owner"] or "").strip().lower() in muted:
            print(f"Skipping {result['name']}: {result['owner']} is muted",
                  flush=True)
            continue
        if not _alert_due(result["name"], now):
            continue
        outcome = send_zulip(settings, result, idle_after=idle_after,
                            stream=stream)
        if outcome == "failed":
            print(f"Failed to alert {result['owner']} about "
                  f"{result['name']}", flush=True)
            continue
        _mark_alerted(result["name"], now)
        sent += 1
        print(f"Alerted {result['owner']} about {result['name']} "
              f"({outcome})", flush=True)
    return sent
