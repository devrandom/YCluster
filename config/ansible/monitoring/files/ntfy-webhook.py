#!/usr/bin/env python3
"""
Alertmanager webhook handler for ntfy notifications.
Receives Alertmanager webhook payloads and forwards them to ntfy with proper formatting.
"""

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque

import requests
from flask import Flask, request, jsonify
from datetime import datetime

try:
    import etcd3
except ImportError:
    etcd3 = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
NTFY_TOPIC = "cf0d6065-2357-493e-a7a8-06666dd660eb"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
# Enrichment queries go to the central Prometheus (not localhost:9090):
# probe_* / https-domain data now lives only on central, which runs on the
# storage leader. admin.xc (10.0.0.100) is the keepalived storage VIP, so it
# follows the leader and reliably reaches central on any core node.
PROMETHEUS_URL = os.environ.get("NTFY_PROMETHEUS_URL", "http://admin.xc:9091")

# Per-alert dedup and global outbound throttle. These exist so bursts of
# Alertmanager retries and repeats don't pile up against ntfy.sh's rate limit
# (429) and extend the block. Values are tunable via env for a rolling restart.
DEDUP_WINDOW_SECONDS = int(os.environ.get("NTFY_DEDUP_WINDOW_SECONDS", "300"))
THROTTLE_MAX_SENDS = int(os.environ.get("NTFY_THROTTLE_MAX_SENDS", "10"))
THROTTLE_WINDOW_SECONDS = int(os.environ.get("NTFY_THROTTLE_WINDOW_SECONDS", "60"))
# Cluster-wide IDS notification budget, per severity. The forwarder is
# per-host, so this shared notification edge is where a fleet-wide cap can
# be enforced. The budget is claim-based in etcd and fails open.
IDS_RATE_LIMIT = int(os.environ.get("NTFY_IDS_RATE_LIMIT", "5"))
IDS_RATE_WINDOW_SECONDS = int(os.environ.get("NTFY_IDS_RATE_WINDOW_SECONDS", "3600"))

_lock = threading.Lock()
_last_sent_at = {}
_send_history = deque()
_shared_client = None


def _new_shared_client():
    """Return an etcd client, or None when shared dedup is unavailable."""
    if etcd3 is None or os.environ.get("NTFY_SHARED_DEDUP", "1") == "0":
        return None

    hosts = [
        value.strip()
        for value in os.environ.get("ETCD_HOSTS", "localhost:2379").split(",")
        if value.strip()
    ]
    kwargs = {}
    for env_name, kwarg_name in [
        ("ETCD_CACERT", "ca_cert"),
        ("ETCD_CERT", "cert_cert"),
        ("ETCD_KEY", "cert_key"),
    ]:
        value = os.environ.get(env_name)
        if value:
            kwargs[kwarg_name] = value

    for host_port in hosts:
        host, _, port = host_port.rpartition(":")
        try:
            client = etcd3.client(
                host=host,
                port=int(port),
                timeout=1,
                **kwargs,
            )
            # gRPC channels connect lazily, so construction alone proves
            # nothing; probe the endpoint so failover can try the next peer.
            client.status()
            return client
        except Exception:
            continue
    return None


def _shared_claim(key):
    """Atomically claim a cross-host send.

    Returns False when another webhook host already claimed the key. On any
    etcd failure it returns True so delivery remains available; callers may
    produce a duplicate rather than miss an alert.
    """
    global _shared_client
    if etcd3 is None or os.environ.get("NTFY_SHARED_DEDUP", "1") == "0":
        return True

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    etcd_key = f"/monitoring/ntfy/notifications/{digest}"

    client = _shared_client
    if client is None:
        client = _new_shared_client()
        if client is None:
            return True
        _shared_client = client

    try:
        lease = client.lease(DEDUP_WINDOW_SECONDS)
        return client.put_if_not_exists(etcd_key, "1", lease=lease)
    except Exception as e:
        _shared_client = None
        logger.warning(f"Shared dedup unavailable; failing open: {e}")
        return True


def _ids_shared_client():
    """Return the shared etcd client for IDS budget state, or None."""
    global _shared_client
    if etcd3 is None or os.environ.get("NTFY_SHARED_DEDUP", "1") == "0":
        return None
    if _shared_client is None:
        _shared_client = _new_shared_client()
    return _shared_client


def _ids_bucket(severity, now=None):
    if now is None:
        now = time.time()
    bucket = int(now // IDS_RATE_WINDOW_SECONDS)
    safe_severity = "".join(c if c.isalnum() else "_" for c in severity)[:64] or "unknown"
    return safe_severity, bucket


def _ids_cap_alert_claim(severity, now=None):
    """Claim the one cap-reached notice allowed per severity and window.

    Returns the etcd key on success, so the caller can release it if the
    notice could not be delivered. Returns None when the notice was already
    claimed or shared state is unavailable.
    """
    if IDS_RATE_LIMIT <= 0:
        return None
    client = _ids_shared_client()
    if client is None:
        return None
    safe_severity, bucket = _ids_bucket(severity, now)
    key = f"/monitoring/ntfy/ids-budget/{safe_severity}/{bucket}/cap"
    try:
        value, _ = client.get(key)
        if value is not None:
            return None
        lease = client.lease(IDS_RATE_WINDOW_SECONDS * 2)
        if client.put_if_not_exists(key, "1", lease=lease):
            return key
        try:
            lease.revoke()
        except Exception:
            pass
        return None
    except Exception as e:
        _shared_client = None
        logger.warning(f"Shared IDS cap notice unavailable; failing open: {e}")
        return None


def _ids_cap_alert_release(key):
    global _shared_client
    if not key or _shared_client is None:
        return
    try:
        _shared_client.delete(key)
    except Exception as e:
        _shared_client = None
        logger.warning(f"Could not release IDS cap notice claim: {e}")


def _ids_budget_claim(severity, now=None):
    """Claim one cluster-wide IDS notification slot for a severity.

    Each slot is a separate etcd key, so the claim is atomic without a
    read-modify-write race. Keys include the fixed window bucket and expire
    after two windows. On any etcd failure the claim fails open: a missed
    security alert is worse than a burst during a control-plane outage.
    """
    if IDS_RATE_LIMIT <= 0:
        return True
    client = _ids_shared_client()
    if client is None:
        return True

    safe_severity, bucket = _ids_bucket(severity, now)
    try:
        for slot in range(IDS_RATE_LIMIT):
            key = f"/monitoring/ntfy/ids-budget/{safe_severity}/{bucket}/{slot}"
            value, _ = client.get(key)
            if value is not None:
                continue
            # Avoid creating an empty lease when another webhook wins the
            # race for this slot.
            lease = client.lease(IDS_RATE_WINDOW_SECONDS * 2)
            if client.put_if_not_exists(key, "1", lease=lease):
                return True
            try:
                lease.revoke()
            except Exception:
                pass
        return False
    except Exception as e:
        _shared_client = None
        logger.warning(f"Shared IDS budget unavailable; failing open: {e}")
        return True


def _format_window():
    if IDS_RATE_WINDOW_SECONDS % 3600 == 0:
        return f"{IDS_RATE_WINDOW_SECONDS // 3600}h"
    return f"{IDS_RATE_WINDOW_SECONDS}s"


def send_ids_cap_alert(severity, alert):
    """Notify once that a severity's cluster-wide IDS budget is exhausted."""
    rule = alert.get("labels", {}).get("rule_id", "")
    summary = alert.get("annotations", {}).get("summary", "unknown")
    if severity == "critical":
        title = "[CRITICAL] IDS alert budget reached"
        priority = "urgent"
        tags = "rotating_light"
    elif severity == "warning":
        title = "[WARNING] IDS alert budget reached"
        priority = "default"
        tags = "warning"
    else:
        title = "[INFO] IDS alert budget reached"
        priority = "low"
        tags = "information_source"
    message = (
        f"The cluster-wide {severity} IDS alert budget of {IDS_RATE_LIMIT} "
        f"per {_format_window()} is exhausted. Further {severity} alerts will "
        "be recorded in detections.jsonl but not delivered until the next "
        f"window.\nFirst suppressed alert: {summary}"
    )
    if rule:
        message += f" (rule_id={rule})"
    return send_to_ntfy(title, message, priority, tags)


def alert_key(alert):
    """Return a stable key identifying one (status, alert) instance."""
    fingerprint = alert.get("fingerprint")
    if fingerprint:
        # Alertmanager keeps the same fingerprint when an alert resolves, so
        # include status or else resolved notifications are deduped as their
        # corresponding firing notification within this short-lived window.
        return f"{alert.get('status')}:{fingerprint}"
    labels = alert.get("labels", {})
    return json.dumps((alert.get("status"), sorted(labels.items())), sort_keys=True)


def _prune_send_history_locked(now):
    while _send_history and now - _send_history[0] > THROTTLE_WINDOW_SECONDS:
        _send_history.popleft()


def _check_admission(key, now=None):
    """Check dedup and the local send throttle without consuming a slot."""
    now = now if now is not None else time.monotonic()
    with _lock:
        last = _last_sent_at.get(key)
        if last is not None and now - last < DEDUP_WINDOW_SECONDS:
            return "dedup"
        _prune_send_history_locked(now)
        if len(_send_history) >= THROTTLE_MAX_SENDS:
            return "throttle"
        return "send"


def _record_admission(key, now=None):
    """Reserve the local send slot once the alert has passed every filter."""
    now = now if now is not None else time.monotonic()
    with _lock:
        _prune_send_history_locked(now)
        if len(_send_history) >= THROTTLE_MAX_SENDS:
            return False
        _last_sent_at[key] = now
        _send_history.append(now)
        return True


def _admit(key, now=None):
    """Decide whether an alert may hit ntfy.sh: 'send', 'dedup', or 'throttle'."""
    decision = _check_admission(key, now)
    if decision != "send":
        return decision
    return "send" if _record_admission(key, now) else "throttle"

def query_blackbox_nodes(domain, instance):
    """Query Prometheus for blackbox_node labels for a given domain/instance."""
    try:
        # Query for all blackbox nodes that have probed this target
        query = f'probe_success{{job="https-domain",domain="{domain}",instance="{instance}"}}'
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={'query': query},
            timeout=5
        )
        response.raise_for_status()
        
        data = response.json()
        if data.get('status') == 'success' and data.get('data', {}).get('result'):
            blackbox_nodes = []
            for result in data['data']['result']:
                blackbox_node = result.get('metric', {}).get('blackbox_node')
                if blackbox_node:
                    blackbox_nodes.append(blackbox_node)
            return blackbox_nodes
        return []
    except Exception as e:
        logger.error(f"Failed to query Prometheus for blackbox nodes: {e}")
        return []

def format_alert_message(alert):
    """Format a single alert for ntfy."""
    status = alert.get('status', 'unknown')
    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})
    
    alertname = labels.get('alertname', 'Unknown Alert')
    severity = labels.get('severity', 'info')
    node = labels.get('node') or None
    
    summary = annotations.get('summary', f'{alertname}' + (f' on {node}' if node else ''))
    description = annotations.get('description', 'No description available')
    
    # Format timestamp - use endsAt for resolved, startsAt for firing
    if status == 'resolved':
        timestamp_field = alert.get('endsAt', '')
    else:
        timestamp_field = alert.get('startsAt', '')
    
    if timestamp_field:
        try:
            dt = datetime.fromisoformat(timestamp_field.replace('Z', '+00:00'))
            timestamp = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except:
            timestamp = timestamp_field
    else:
        timestamp = 'Unknown time'
    
    # Check if this alert has domain/instance labels (indicates blackbox monitoring)
    domain = labels.get('domain')
    instance = labels.get('instance')
    blackbox_info = ""
    
    if domain and instance:
        blackbox_nodes = query_blackbox_nodes(domain, instance)
        if blackbox_nodes:
            blackbox_info = f"\nBlackbox nodes: {', '.join(blackbox_nodes)}"
        else:
            blackbox_info = "\nBlackbox nodes: (query failed or no data)"
    
    if status == 'resolved':
        title = f"[RESOLVED] {summary}"
        message = f"Alert resolved at {timestamp}\n\n{description}{blackbox_info}"
        priority = "default"
        tags = "white_check_mark"
    else:
        if severity == 'critical':
            title = f"[CRITICAL] {summary}"
            priority = "urgent"
            tags = "rotating_light"
        elif severity == 'warning':
            title = f"[WARNING] {summary}"
            priority = "default"
            tags = "warning"
        else:
            title = f"[INFO] {summary}"
            priority = "low"
            tags = "information_source"
        
        # Build message with optional node line
        message_parts = [f"Alert started at {timestamp}"]
        if node is not None:
            message_parts.append(f"Node: {node}")
        message_parts.append(f"Severity: {severity}")
        message_parts.append("")
        message_parts.append(description)
        if blackbox_info:
            message_parts.append(blackbox_info)
        
        message = "\n".join(message_parts)
    
    return {
        'title': title,
        'message': message,
        'priority': priority,
        'tags': tags
    }

def send_to_ntfy(title, message, priority="default", tags=""):
    """Send a notification to ntfy. Returns True on success, 'rate_limited'
    when ntfy.sh returned HTTP 429, or False on any other failure."""
    headers = {
        'Title': title.encode('utf-8').decode('utf-8'),
        'Priority': priority,
        'Tags': tags,
        'Content-Type': 'text/plain; charset=utf-8'
    }
    
    try:
        # Ensure message is properly encoded as UTF-8
        message_bytes = message.encode('utf-8')
        response = requests.post(NTFY_URL, data=message_bytes, headers=headers, timeout=10)
        if response.status_code == 429:
            logger.warning(f"ntfy.sh rate-limited notification: {title}")
            return 'rate_limited'
        response.raise_for_status()
        logger.info(f"Sent notification to ntfy: {title}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send notification to ntfy: {e}")
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Alertmanager webhook."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400

        logger.info(f"Received webhook with data: {json.dumps(data, indent=2)}")

        alerts = data.get('alerts', [])
        counts = _new_counts()
        for alert in alerts:
            _handle_alert(alert, counts)
        return _webhook_response(len(alerts), counts)

    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        return jsonify({'error': str(e)}), 500


COUNT_KEYS = ('sent', 'deduped', 'throttled', 'rate_limited', 'cap_reached', 'errors')


def _new_counts():
    return dict.fromkeys(COUNT_KEYS, 0)


def _handle_alert(alert, counts):
    """Admit, throttle or send one alert, updating the summary counters."""
    key = alert_key(alert)
    decision = _check_admission(key)
    if decision == 'dedup':
        counts['deduped'] += 1
        logger.info(f"Deduplicated already-notified alert: {key}")
        return
    if decision == 'throttle':
        counts['throttled'] += 1
        logger.warning(f"Throttled notification for alert: {key}")
        return
    if decision == 'send' and not _shared_claim(key):
        counts['deduped'] += 1
        logger.info(f"Deduplicated cross-host notification: {key}")
        return
    if _ids_budget_exhausted(alert):
        _handle_ids_cap(alert, key, counts)
        return
    if not _record_admission(key):
        counts['throttled'] += 1
        logger.warning(f"Throttled notification for alert: {key}")
        return
    _send_alert(alert, counts)


def _ids_budget_exhausted(alert):
    """Report whether an IDS alert is over the cluster-wide budget."""
    labels = alert.get("labels", {})
    if alert.get("status", "firing") != "firing":
        return False
    if labels.get("alertname") != "IDSAlert":
        return False
    if labels.get("rule_id") == "container-api":
        return False
    return not _ids_budget_claim(labels.get("severity", "info"))


def _handle_ids_cap(alert, key, counts):
    """Throttle one over-budget IDS alert, sending the cap notice at most once."""
    severity = alert.get("labels", {}).get("severity", "info")
    counts['throttled'] += 1
    cap_key = _ids_cap_alert_claim(severity)
    if not cap_key:
        logger.warning(f"IDS {severity} budget exhausted; throttled: {key}")
        return
    try:
        result = send_ids_cap_alert(severity, alert)
    except Exception as e:
        logger.error(f"Error sending IDS cap notice: {e}")
        result = False
    if result is True:
        counts['sent'] += 1
        counts['cap_reached'] += 1
        logger.warning(f"IDS {severity} budget exhausted; sent cap notice: {key}")
        return
    if result == 'rate_limited':
        counts['rate_limited'] += 1
        counts['cap_reached'] += 1
    else:
        counts['errors'] += 1
        _ids_cap_alert_release(cap_key)
    logger.warning(f"IDS {severity} budget exhausted; cap notice not delivered: {key}")


def _send_alert(alert, counts):
    try:
        formatted = format_alert_message(alert)
        result = send_to_ntfy(
            formatted['title'],
            formatted['message'],
            formatted['priority'],
            formatted['tags']
        )
        if result is True:
            counts['sent'] += 1
        elif result == 'rate_limited':
            counts['rate_limited'] += 1
        else:
            counts['errors'] += 1
    except Exception as e:
        logger.error(f"Error processing alert: {e}")
        counts['errors'] += 1


def _webhook_response(processed, counts):
    """Acknowledge everything except a total delivery failure.

    Deduped/throttled/rate-limited alerts are acknowledged so Alertmanager
    does not retry them and re-stoke the ntfy.sh rate limit. Only a genuine
    send failure with nothing delivered keeps the 503 -> retry behaviour.
    """
    if counts['errors'] and counts['sent'] == 0:
        return jsonify({
            'error': 'Failed to send notifications',
            'processed': processed,
            'sent': 0,
            'deduped': counts['deduped'],
            'throttled': counts['throttled'],
            'rate_limited': counts['rate_limited'],
            'cap_reached': counts['cap_reached'],
            'errors': counts['errors']
        }), 503  # Service Unavailable - triggers Alertmanager retry

    return jsonify({
        'status': 'success',
        'processed': processed,
        **counts
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'healthy', 'service': 'ntfy-webhook'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9095, debug=False)
