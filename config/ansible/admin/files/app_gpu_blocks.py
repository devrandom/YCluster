"""Helpers for admin-managed GPU block mutations."""

import re
import secrets
from datetime import datetime, timezone


def validate_gpu_block(data):
    """Validate host, GPU count, and mode; return (fields, response).

    ``fields`` is None when the request is invalid. ``response`` is a
    (payload, HTTP status) tuple suitable for Flask's ``jsonify`` caller.
    """
    host = str(data.get('host', '')).strip()
    gpus = data.get('gpus')
    mode = data.get('mode')

    if not re.fullmatch(r'[a-z0-9-]+', host):
        return None, ({'error': 'invalid host'}, 400)
    if not isinstance(gpus, int) or isinstance(gpus, bool) or \
            not 1 <= gpus <= 512:
        return None, ({'error': 'gpus must be a positive integer'}, 400)
    if mode not in ('on', 'schedule'):
        return None, ({'error': 'mode must be on|schedule'}, 400)
    return {
        'host': host,
        'gpus': gpus,
        'mode': mode,
        'reason': (data.get('reason') or '').strip()[:200],
        'block_id': data.get('block') or '',
    }, None


def resolve_gpu_block(block_id, vmm):
    """Load an existing block or mint a new ID; return (id, current, response)."""
    if not block_id:
        return (f"block-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-"
                f"{secrets.token_hex(3)}"), None, None
    if not re.fullmatch(r'[a-z0-9-]+', block_id):
        return None, None, ({'error': 'no such block'}, 404)
    try:
        current = vmm.gpu_block_get(block_id)
    except Exception as exc:
        return None, None, ({'error': f'etcd connection failed: {exc}'}, 503)
    if not current:
        return None, None, ({'error': 'no such block'}, 404)
    return block_id, current, None


def invalid_windows(mode, windows):
    """Schedule blocks must contain at least one parsed window."""
    if windows is None or (mode == 'schedule' and not windows):
        return {'error': 'invalid windows'}, 400
    return None


def gpu_block_record(identity, current, host, gpus, mode, windows, reason,
                     now=None):
    """Create the etcd record, preserving original creation metadata."""
    now = now or datetime.now(timezone.utc)
    return {
        'host': host,
        'gpus': gpus,
        'mode': mode,
        'windows': windows,
        'reason': reason,
        'created_by': identity.email or identity.uid or 'internal',
        'created_at': ((current or {}).get('created_at')
                       or now.isoformat(timespec='seconds')),
        'updated_at': now.isoformat(timespec='seconds'),
    }
