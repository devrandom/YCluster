"""Lease persistence and lifecycle helpers for the DHCP server."""

import json
import logging
from datetime import datetime, timedelta

from .dhcp_packets import LEASE_TIME

ETCD_PREFIX = '/cluster/dhcp'
logger = logging.getLogger(__name__)


class DHCPLeaseStore:
    def migrate_leases_to_normalized_mac(self):
        """Migrate existing leases to use normalized MAC addresses as keys"""
        client = self.get_etcd_client()
        if not client:
            return

        logger.info("Starting lease migration to normalized MAC addresses...")
        migrated_count = 0

        try:
            # Get all existing lease entries
            for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/leases/"):
                if not value:
                    continue

                try:
                    lease_data = json.loads(value.decode('utf-8'))
                    old_key = metadata.key.decode()
                    old_mac_key = old_key.split('/')[-1]  # Extract MAC from key

                    # Get MAC from lease data
                    mac_in_data = lease_data.get('mac', '')
                    if not mac_in_data:
                        logger.warning(f"Lease {old_key} has no MAC in data, skipping")
                        continue

                    # Normalize the MAC address
                    normalized_mac = mac_in_data.lower().replace(':', '').replace('-', '')
                    new_key = f"{ETCD_PREFIX}/leases/{normalized_mac}"

                    # Check if this lease needs migration
                    if old_mac_key == normalized_mac:
                        continue

                    # Store lease with normalized MAC key
                    client.put(new_key, json.dumps(lease_data))

                    # Delete old entry if it's different from new key
                    if old_key != new_key:
                        client.delete(old_key)
                        logger.info(f"Migrated lease: {old_key} -> {new_key}")
                        migrated_count += 1

                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse lease data for {metadata.key.decode()}: {e}")
                except Exception as e:
                    logger.error(f"Failed to migrate lease {metadata.key.decode()}: {e}")

            if migrated_count > 0:
                logger.info(f"Successfully migrated {migrated_count} leases to normalized MAC format")
            else:
                logger.info("No lease migration needed - all leases already use normalized MAC format")

        except Exception as e:
            logger.error(f"Failed to migrate leases: {e}")

    def load_leases_from_etcd(self):
        """Load existing leases from etcd"""
        client = self.get_etcd_client()
        if not client:
            return

        try:
            for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/leases/"):
                lease_data = json.loads(value.decode('utf-8'))
                mac = lease_data['mac']

                # Ensure lease has required fields for new format
                if 'expires' not in lease_data:
                    # Legacy lease - add expires field
                    expires = datetime.now() + timedelta(seconds=LEASE_TIME)
                    lease_data['expires'] = expires.isoformat()

                if 'hostname' not in lease_data:
                    lease_data['hostname'] = ''

                if 'allocated_at' not in lease_data:
                    lease_data['allocated_at'] = datetime.now().isoformat()

                self.leases[mac] = lease_data
                self.allocated_ips.add(lease_data['ip'])
                logger.info(f"Loaded lease: {mac} -> {lease_data['ip']}")
        except Exception as e:
            logger.error(f"Failed to load leases from etcd: {e}")

    def save_lease_to_etcd(self, mac, lease_data):
        """Save lease to etcd using normalized MAC as key"""
        client = self.get_etcd_client()
        if not client:
            return False

        try:
            # Normalize MAC for etcd key
            normalized_mac = mac.lower().replace(':', '').replace('-', '')
            key = f"{ETCD_PREFIX}/leases/{normalized_mac}"
            client.put(key, json.dumps(lease_data))
            logger.info(f"Saved lease to etcd: {mac} -> {lease_data['ip']}")
            return True
        except Exception as e:
            logger.error(f"Failed to save lease to etcd: {e}")
            return False

    def get_or_create_lease(self, mac, requested_ip=None, requested_hostname=None):
        """Get existing lease or create new one using Flask app allocation logic"""
        # Check if we have an existing lease
        if mac in self.leases:
            lease = self.leases[mac]
            # Check if lease is still valid
            expires = datetime.fromisoformat(lease['expires'])
            if expires > datetime.now():
                # If hostname changed and it's not a temporary hostname, we need to reallocate
                if requested_hostname and lease.get('hostname', '') != requested_hostname:
                    # Don't reallocate for temporary hostnames - preserve existing allocation
                    if requested_hostname not in ['ubuntu-server', 'localhost', '']:
                        logger.info(f"Hostname change requested for {mac}: {lease.get('hostname', '')} -> {requested_hostname}")
                        # Remove old lease
                        self.allocated_ips.discard(lease['ip'])
                        del self.leases[mac]
                    else:
                        logger.info(f"Ignoring temporary hostname '{requested_hostname}' for {mac}, keeping existing allocation")
                        return lease['ip'], lease.get('hostname', '')
                else:
                    return lease['ip'], lease.get('hostname', '')
            else:
                # Lease expired, remove it
                self.allocated_ips.discard(lease['ip'])
                del self.leases[mac]

        # Use Flask app allocation logic to get hostname and IP
        hostname, ip = self.allocate_hostname_and_ip(mac, requested_hostname)

        if not ip:
            logger.error(f"No IP could be allocated for {mac}")
            return None, None

        # Create new lease
        expires = datetime.now() + timedelta(seconds=LEASE_TIME)
        lease_data = {
            'mac': mac,
            'ip': ip,
            'hostname': hostname or '',
            'expires': expires.isoformat(),
            'allocated_at': datetime.now().isoformat()
        }

        self.leases[mac] = lease_data
        self.allocated_ips.add(ip)
        self.save_lease_to_etcd(mac, lease_data)

        return ip, hostname
