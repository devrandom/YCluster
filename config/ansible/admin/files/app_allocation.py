"""Hostname/IP allocation helpers shared by the admin API."""

import json
import sys
import threading
from datetime import datetime, UTC
from typing import TypedDict

from ycluster.common.etcd_utils import get_etcd_client

allocation_lock = threading.Lock()
ETCD_PREFIX = '/cluster/nodes'


class IPRange(TypedDict):
    base: int
    max: int

IP_RANGES: dict[str, IPRange] = {
    's': {'base': 10, 'max': 20},    # Storage
    'c': {'base': 50, 'max': 20},    # Compute
    'm': {'base': 90, 'max': 20},    # MacOS
    'nv': {'base': 110, 'max': 20},  # Nvidia
    'nas': {'base': 130, 'max': 10}, # NAS
    'd': {'base': 200, 'max': 30},   # Dev (remote wg scratch)
}

def _parse_host_location(hostname):
    """Return (prefix, number, is_amt) for a known host prefix, or None.

    AMT hostnames end in 'a'; that suffix is stripped before parsing.
    """
    is_amt = hostname.endswith('a')
    bare = hostname[:-1] if is_amt else hostname
    for prefix in IP_RANGES:
        if not bare.startswith(prefix):
            continue
        try:
            return prefix, int(bare[len(prefix):]), is_amt
        except ValueError:
            continue
    return None

def _host_ip(prefix, number, is_amt, via_wg):
    config = IP_RANGES[prefix]
    if not 1 <= number <= config['max']:
        raise ValueError(f"Node number {number} for prefix '{prefix}' exceeds range 1-{config['max']}")
    last_octet = config['base'] + number
    if is_amt:
        # AMT interface: separate 10.10.10.0/24 subnet.
        return f"10.10.10.{last_octet}"
    if via_wg:
        # WG peers live in their own /24 for a clean connected route.
        return f"10.0.1.{last_octet}"
    return f"10.0.0.{last_octet}"

def determine_ip_from_hostname(hostname, via_wg=False):
    """Generate deterministic IP based on hostname.

    via_wg=True puts the IP in the wg peer subnet (10.0.1.0/24) while
    preserving the per-type hostname numbering. A physical compute c3
    lands at 10.0.0.53; a wg-bootstrapped compute c3 lands at 10.0.1.53.
    """
    parsed = _parse_host_location(hostname) if hostname else None
    if parsed is None:
        return None
    return _host_ip(*parsed, via_wg=via_wg)

def determine_type_from_mac(mac_address):
    """Determine machine type based on MAC address prefix.

    Used for PXE boot allocation. macOS nodes use explicit type parameter
    via /macos/bootstrap endpoint.
    """
    if not mac_address:
        return 'compute'

    # Normalize MAC address to lowercase and remove separators
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

    # Check for storage prefix (58:47:ca becomes 5847ca)
    if normalized_mac.startswith('5847ca'):
        return 'storage'

    # Default to compute
    return 'compute'

def get_next_hostname(client, node_type):
    """Get the next available hostname for a node type"""
    prefixes = {
        'storage': 's',
        'compute': 'c',
        'macos': 'm',
        'nvidia': 'nv',
        'nas': 'nas',
        'dev': 'd',
    }
    
    prefix = prefixes.get(node_type, 'c')
    
    # Get all existing hostnames of this type
    existing_numbers = []
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/{prefix}"):
        if value:
            hostname = metadata.key.decode().split('/')[-1]
            try:
                num = int(hostname[len(prefix):])
                existing_numbers.append(num)
            except:
                pass
    
    # Find the next available number
    next_num = 1
    if existing_numbers:
        existing_numbers.sort()
        # Find first gap or use max+1
        for i, num in enumerate(existing_numbers):
            if num != i + 1:
                next_num = i + 1
                break
        else:
            next_num = len(existing_numbers) + 1
    
    return f"{prefix}{next_num}"


def get_or_create_allocation(mac_address, node_type=None, via_wg=False):
    """Get existing allocation or create new one for non-normalized MAC address.

    Args:
        mac_address: MAC address (any format)
        node_type: Optional type override ('storage', 'compute', 'macos').
                   If not provided, type is detected from MAC address.
                   If provided and different from existing, the allocation is updated.
        via_wg:    Transport marker — WG-bootstrapped nodes get their IP
                   from 10.0.1.0/24 instead of 10.0.0.0/24. Preserved in
                   the allocation record so list/lookup stays consistent.
    """
    client = get_etcd_client()
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

    # Check if allocation already exists
    existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
    if existing_data[0]:
        data = json.loads(existing_data[0].decode())
        existing_via_wg = bool(data.get('via_wg', False))
        # Some old entries may lack IP or amt_ip, fill them in
        if 'amt_ip' not in data:
            amt_ip_address = determine_ip_from_hostname(data['hostname'] + 'a')
            data['amt_ip'] = amt_ip_address

        # Transport changed (LAN <-> WG): IP is derived from hostname+via_wg,
        # so recompute and rewrite. Otherwise a node bootstrapped over WG after
        # a prior LAN allocation keeps its 10.0.0.x address, which collides
        # with the LAN subnet on the WG server and silently breaks the tunnel.
        if via_wg != existing_via_wg:
            new_ip = determine_ip_from_hostname(data['hostname'], via_wg=via_wg)
            print(f"allocation {data['hostname']} ({normalized_mac}): transport "
                  f"{'LAN->WG' if via_wg else 'WG->LAN'}, ip {data['ip']} -> {new_ip}",
                  file=sys.stderr)
            data['ip'] = new_ip
            data['via_wg'] = via_wg
            data['updated_at'] = datetime.now(UTC).isoformat()
            existing_via_wg = via_wg
            allocation_json = json.dumps(data)
            client.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json)
            client.put(f"{ETCD_PREFIX}/by-hostname/{data['hostname']}", allocation_json)

        # Update type if explicitly requested and different
        if node_type and data.get('type') != node_type:
            old_hostname = data['hostname']
            # Allocate new hostname for the new type
            new_hostname = get_next_hostname(client, node_type)
            new_ip = determine_ip_from_hostname(new_hostname, via_wg=existing_via_wg)
            new_amt_ip = determine_ip_from_hostname(new_hostname + "a")

            # Update allocation data
            data['hostname'] = new_hostname
            data['type'] = node_type
            data['ip'] = new_ip
            data['amt_ip'] = new_amt_ip
            data['updated_at'] = datetime.now(UTC).isoformat()

            # Update in etcd (delete old hostname entry, create new one)
            allocation_json = json.dumps(data)
            client.delete(f"{ETCD_PREFIX}/by-hostname/{old_hostname}")
            client.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json)
            client.put(f"{ETCD_PREFIX}/by-hostname/{new_hostname}", allocation_json)

        return data


    # Create new allocation
    with allocation_lock:
        # Double-check after acquiring lock
        existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
        if existing_data[0]:
            return json.loads(existing_data[0].decode())

        # Determine machine type and allocate hostname
        machine_type = node_type or determine_type_from_mac(mac_address)

        # allocation_lock only serializes this process; the DHCP server (and
        # any other admin-api instance) allocates concurrently, so commit via
        # compare-and-swap and retry on conflict.
        for _ in range(5):
            hostname = get_next_hostname(client, machine_type)
            ip_address = determine_ip_from_hostname(hostname, via_wg=via_wg)
            amt_ip_address = determine_ip_from_hostname(hostname + "a")

            allocation_data = {
                'hostname': hostname,
                'type': machine_type,
                'ip': ip_address,
                'amt_ip': amt_ip_address,
                'mac': normalized_mac,
                'via_wg': via_wg,
                'allocated_at': datetime.now(UTC).isoformat()
            }

            allocation_json = json.dumps(allocation_data)
            committed, _ = client.transaction(
                compare=[
                    client.transactions.version(f"{ETCD_PREFIX}/by-mac/{normalized_mac}") == 0,
                    client.transactions.version(f"{ETCD_PREFIX}/by-hostname/{hostname}") == 0
                ],
                success=[
                    client.transactions.put(f"{ETCD_PREFIX}/by-mac/{normalized_mac}", allocation_json),
                    client.transactions.put(f"{ETCD_PREFIX}/by-hostname/{hostname}", allocation_json)
                ],
                failure=[]
            )
            if committed:
                return allocation_data

            # Lost the race. If this MAC got allocated elsewhere, return that
            # allocation; otherwise the hostname was taken — pick the next.
            existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
            if existing_data[0]:
                return json.loads(existing_data[0].decode())

        raise RuntimeError(
            f"allocation for {normalized_mac} failed: etcd transaction "
            f"conflicted on every attempt")
