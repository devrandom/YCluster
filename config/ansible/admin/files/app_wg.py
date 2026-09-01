"""Helpers for the public WireGuard onboarding endpoint."""

import base64

WG_NODE_TYPES = ('storage', 'compute', 'macos', 'nas', 'nvidia', 'dev')


def validate_register_input(data):
    """Return (mac, node_type, pubkey, error) from a JSON request body."""
    mac = data.get('mac')
    node_type = data.get('type') or 'compute'
    pubkey = data.get('pubkey')

    if not mac or not pubkey:
        return None, None, None, 'mac and pubkey are required'
    if node_type not in WG_NODE_TYPES:
        return None, None, None, f'invalid type: {node_type}'
    if not valid_pubkey(pubkey):
        return None, None, None, 'invalid pubkey (expected base64 32-byte key)'
    return mac, node_type, pubkey, None


def valid_pubkey(pubkey):
    """Check a base64-encoded 32-byte WireGuard public key."""
    try:
        return len(base64.b64decode(pubkey, validate=True)) == 32
    except Exception:
        return False


def register_peer(allocation_getter, peer_register, mac, node_type, pubkey):
    """Allocate a node and register its peer, returning (allocation, peer, error)."""
    try:
        allocation = allocation_getter(mac, node_type=node_type, via_wg=True)
    except Exception as e:
        return None, None, (500, f'allocation failed: {e}')
    try:
        return allocation, peer_register(allocation['hostname'], pubkey), None
    except ValueError as e:
        return None, None, (400, str(e))
    except Exception as e:
        return None, None, (500, f'registration failed: {e}')
