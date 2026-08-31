"""Hostname and IP allocation helpers for the DHCP server."""

import json
import logging
import socket
import subprocess
from datetime import datetime, timezone
from typing import TypedDict, cast

import yaml

from etcd3.client import KVMetadata


class IPRange(TypedDict):
    base: int
    max: int


IP_RANGES: dict[str, IPRange] = {
    's': {'base': 10, 'max': 20},    # Storage: s1-s20
    'c': {'base': 50, 'max': 20},    # Compute: c1-c20
    'm': {'base': 90, 'max': 20},    # MacOS: m1-m20
    'nv': {'base': 110, 'max': 20},  # Nvidia: nv1-nv20
    'nas': {'base': 130, 'max': 10}, # NAS: nas1-nas10
    'x': {'base': 150, 'max': 49},   # Adhoc: x1-x49
}
DYNAMIC_IP_START = 200
DYNAMIC_IP_END = 249

# Sentinel: an allocation attempt lost its etcd compare-and-swap race and
# should be re-run from the top (re-check existing state, re-pick, re-commit).
_ALLOC_RETRY = object()
logger = logging.getLogger(__name__)


class DHCPAllocationMixin:
    def get_server_ip(self):
        """Get the IP address of the DHCP server from netplan primary interface"""
        try:
            # Try to get primary interface from netplan
            result = subprocess.run(['netplan', 'get', 'network.ethernets.primary'],
                                  capture_output=True, text=True, check=True)
            primary_config = yaml.safe_load(result.stdout)

            if primary_config and 'addresses' in primary_config:
                # Get first address and extract IP (remove CIDR notation)
                first_addr = primary_config['addresses'][0]
                ip = first_addr.split('/')[0]
                logger.info(f"Using primary interface IP from netplan: {ip}")
                return ip
        except (subprocess.CalledProcessError, yaml.YAMLError, IndexError, KeyError) as e:
            logger.warning(f"Could not get primary interface from netplan: {e}")

        # Fallback: derive the IP from the hostname convention
        hostname = socket.gethostname()
        ip = self.determine_ip_from_hostname(hostname)
        if ip:
            logger.info(f"Using convention IP for {hostname}: {ip}")
            return ip

        raise ValueError("Could not determine server IP from netplan primary interface or hostname")

    def determine_ip_from_hostname(self, hostname):
        """Generate deterministic IP based on hostname (same logic as Flask app)"""
        if not hostname:
            return None

        # Check if this is an AMT hostname (ends with 'a')
        is_amt = hostname.endswith('a')
        if is_amt:
            hostname = hostname[:-1]  # Strip trailing 'a' for parsing

        # Try multi-char prefixes first (e.g., 'nas'), then single-char
        prefix = None
        num = None
        for p in IP_RANGES:
            if hostname.startswith(p):
                try:
                    num = int(hostname[len(p):])
                    prefix = p
                    break
                except ValueError:
                    continue

        if prefix is None:
            return None
        assert num is not None  # narrowed by the loop+break: prefix set only when num set

        config = IP_RANGES[prefix]

        # Validate number is within range
        if num < 1 or num > config['max']:
            return None

        # Calculate base IP address
        base_ip = config['base'] + num

        if is_amt:
            # AMT interface: use the separate AMT subnet
            return f"10.10.10.{base_ip}"
        else:
            # Regular interface
            return f"10.0.0.{base_ip}"

    def determine_type_from_mac(self, mac_address):
        """Determine machine type based on MAC address prefix (same logic as Flask app)"""
        if not mac_address:
            return 'compute'

        # Normalize MAC address to lowercase and remove separators
        normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

        # Check for storage prefix (58:47:ca becomes 5847ca)
        if normalized_mac.startswith('5847ca'):
            return 'storage'

        # Default to compute
        return 'compute'

    def determine_type_from_hostname(self, hostname):
        """Determine machine type from hostname prefix"""
        if not hostname:
            return 'compute'

        # Check multi-char prefixes first
        if hostname.startswith('nas'):
            return 'nas'
        if hostname.startswith('nv'):
            return 'nvidia'

        prefix = hostname[0].lower()
        if prefix == 's':
            return 'storage'
        elif prefix == 'c':
            return 'compute'
        elif prefix == 'm':
            return 'macos'
        elif prefix == 'x':
            return 'adhoc'
        else:
            return 'compute'

    def get_next_dynamic_ip(self):
        """Get the next available IP from the dynamic range 200-249"""
        client = self.get_etcd_client()
        assert client is not None

        # Get all allocated IPs from etcd nodes
        allocated_ips = set()
        # etcd3's get_prefix returns Optional[Generator] per its inferred type
        # (the @_handle_errors wrapper drops the return annotation), but
        # at runtime it always yields (value, metadata) tuples.
        entries = cast("list[tuple[bytes, KVMetadata]]",
                       list(client.get_prefix(f"/cluster/nodes/by-hostname/") or ()))
        for value, metadata in entries:
            if value:
                try:
                    allocation = json.loads(value.decode())
                    ip = allocation['ip']
                    # Extract last octet
                    last_octet = int(ip.split('.')[-1])
                    allocated_ips.add(last_octet)
                except:
                    pass

        # Also include currently leased IPs
        for lease in self.leases.values():
            try:
                last_octet = int(lease['ip'].split('.')[-1])
                allocated_ips.add(last_octet)
            except:
                pass

        # Find next available IP in dynamic range
        for ip_num in range(DYNAMIC_IP_START, DYNAMIC_IP_END + 1):
            if ip_num not in allocated_ips:
                return f"10.0.0.{ip_num}"

        # If no IPs available, return None
        return None

    def get_next_hostname(self, node_type):
        """Get the next available hostname for a node type (same logic as Flask app)"""
        prefixes = {
            'storage': 's',
            'compute': 'c',
            'macos': 'm',
            'nvidia': 'nv',
            'nas': 'nas',
            'adhoc': 'x'
        }

        prefix = prefixes.get(node_type, 'c')

        client = self.get_etcd_client()
        if not client:
            return f"{prefix}1"  # Fallback

        # Get all existing hostnames of this type from etcd nodes prefix
        existing_numbers = []
        for value, metadata in client.get_prefix(f"/cluster/nodes/by-hostname/{prefix}"):
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

    def allocate_hostname_and_ip(self, mac_address, requested_hostname=None):
        """Allocate hostname and IP using same logic as Flask app"""
        # The commit is a compare-and-swap; another allocator (admin-api, or
        # a second dhcp-server during leader failover) can win the race, in
        # which case the whole check/pick/commit cycle is re-run.
        for _ in range(3):
            result = self._allocate_attempt(mac_address, requested_hostname)
            if result is not _ALLOC_RETRY:
                return result
        logger.error(f"Allocation for {mac_address} conflicted on every attempt")
        return None, None

    def _allocate_attempt(self, mac_address, requested_hostname=None):
        """One allocation attempt: returns (hostname, ip), (None, None) on
        fatal error, or _ALLOC_RETRY on a lost etcd race."""
        # Normalize MAC address
        normalized_mac = mac_address.lower().replace(':', '').replace('-', '')

        # Don't allocate AMT hostnames via DHCP - they are static only
        if requested_hostname and requested_hostname.endswith('a'):
            logger.info(f"Ignoring AMT hostname request {requested_hostname} - AMT interfaces use static configuration")
            requested_hostname = None

        client = self.get_etcd_client()
        if not client:
            return None, None

        # Check if this MAC address already has an allocation in etcd nodes
        existing_data = client.get(f"/cluster/nodes/by-mac/{normalized_mac}")
        if existing_data[0]:
            allocation = json.loads(existing_data[0].decode())
            # If no hostname requested or same hostname, return existing
            if not requested_hostname or allocation['hostname'] == requested_hostname:
                return allocation['hostname'], allocation['ip']
            # If different hostname requested, we need to reallocate
            logger.info(f"Reallocating {mac_address} from {allocation['hostname']} to {requested_hostname}")

        hostname = None
        ip_address = None
        machine_type = None
        # mod_revision of an existing by-hostname entry we validated as ours
        # (same MAC); None means the hostname must still be unallocated at
        # commit time.
        hostname_mod_revision = None

        # Use requested hostname if provided and valid
        if requested_hostname:
            # Validate hostname format and calculate IP
            ip_address = self.determine_ip_from_hostname(requested_hostname)
            if ip_address:
                # Check if hostname is already taken by another MAC
                existing_hostname_data = client.get(f"/cluster/nodes/by-hostname/{requested_hostname}")
                existing_value, existing_metadata = existing_hostname_data
                if existing_value:
                    existing_allocation = json.loads(existing_value.decode())
                    if existing_allocation['mac'] != mac_address:
                        logger.warning(f"Hostname {requested_hostname} already taken by {existing_allocation['mac']}")
                        # Fall back to auto-allocation
                        requested_hostname = None
                        hostname = None
                        ip_address = None
                    else:
                        # Same MAC, update allocation
                        hostname = requested_hostname
                        machine_type = self.determine_type_from_hostname(hostname)
                        assert existing_metadata is not None
                        hostname_mod_revision = existing_metadata.mod_revision
                else:
                    # Hostname available, use it
                    hostname = requested_hostname
                    machine_type = self.determine_type_from_hostname(hostname)
            else:
                logger.warning(f"Invalid hostname format: {requested_hostname}, falling back to auto-allocation")
                requested_hostname = None

        # Fall back to auto-allocation if no valid hostname requested or validation failed
        if not hostname:
            # For auto-allocation, use dynamic IP range instead of hostname-based IPs
            ip_address = self.get_next_dynamic_ip()
            if not ip_address:
                logger.error(f"No dynamic IPs available in range {DYNAMIC_IP_START}-{DYNAMIC_IP_END}")
                return None, None

            # Create a temporary hostname based on IP for tracking
            last_octet = ip_address.split('.')[-1]
            hostname = f"dhcp-{last_octet}"
            machine_type = self.determine_type_from_mac(mac_address)

        # Create allocation record
        allocation = {
            'hostname': hostname,
            'type': machine_type,
            'ip': ip_address,
            'mac': normalized_mac,
            'allocated_at': datetime.now(timezone.utc).isoformat()
        }

        # Store in etcd with both lookups (same as Flask app)
        allocation_json = json.dumps(allocation)

        try:
            compare, success = self._transaction_parts(
                client, normalized_mac, existing_data, hostname,
                hostname_mod_revision, allocation_json)
            self._retire_old_hostname(
                client, existing_data, hostname, success)
            committed, _ = client.transaction(
                compare=compare, success=success, failure=[])
            if not committed:
                logger.warning(f"Allocation of {hostname} to {mac_address} "
                               f"lost an etcd race; retrying")
                return _ALLOC_RETRY
            logger.info(f"Allocated {hostname} ({ip_address}) to {mac_address}")
            return hostname, ip_address
        except Exception as e:
            logger.error(f"Failed to store allocation in etcd: {e}")
            return None, None

    def _transaction_parts(self, client, normalized_mac, existing_data,
                           hostname, hostname_mod_revision, allocation_json):
        """Build the compares and writes guarding a non-atomic allocation."""
        # Compare-and-swap: the checks above are non-atomic with this write,
        # so guard them with compares — by-mac unchanged since we read it (or
        # still absent), and the target hostname still in the state we
        # validated. A concurrent allocator makes the transaction fail rather
        # than silently double-assigning.
        if existing_data[0]:
            assert existing_data[1] is not None
            compare = [client.transactions.mod(
                f"/cluster/nodes/by-mac/{normalized_mac}")
                == existing_data[1].mod_revision]
        else:
            compare = [client.transactions.version(
                f"/cluster/nodes/by-mac/{normalized_mac}") == 0]
        if hostname_mod_revision is not None:
            compare.append(client.transactions.mod(
                f"/cluster/nodes/by-hostname/{hostname}")
                == hostname_mod_revision)
        else:
            compare.append(client.transactions.version(
                f"/cluster/nodes/by-hostname/{hostname}") == 0)

        success: list = [
            client.transactions.put(f"/cluster/nodes/by-mac/{normalized_mac}",
                                    allocation_json),
            client.transactions.put(f"/cluster/nodes/by-hostname/{hostname}",
                                    allocation_json),
        ]
        return compare, success

    def _retire_old_hostname(self, client, existing_data, hostname, success):
        """Queue deletion of the previous hostname entry when reallocating."""
        # Retire the old hostname entry in the same transaction (deleting it
        # up front would orphan the record if the writes then failed).
        if not existing_data[0]:
            return
        old_hostname = json.loads(existing_data[0].decode())['hostname']
        if old_hostname != hostname:
            success.append(client.transactions.delete(
                f"/cluster/nodes/by-hostname/{old_hostname}"))
