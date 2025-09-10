#!/usr/bin/env python3
"""
Scapy-based DHCP server with etcd integration for YCluster
"""

import os
import sys
import time
import json
import socket
import threading
import logging
import subprocess
import yaml
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from scapy.all import *
from scapy.layers.dhcp import DHCP, BOOTP
from scapy.layers.inet import IP, UDP
from scapy.layers.l2 import Ether

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from common.etcd_utils import get_etcd_client, get_etcd_hosts

# Configuration
ETCD_PREFIX = '/cluster/dhcp'
LEASE_TIME = 43200  # 12 hours
GATEWAY = '10.0.0.254'
DNS_SERVER = '10.0.0.254'
NTP_SERVER = '10.0.0.254'
HEALTH_PORT = int(os.environ.get('DHCP_HEALTH_PORT', '8067'))

# IP allocation configuration (same as Flask app)
IP_RANGES = {
    's': {'base': 10, 'max': 20},    # Storage: 10.0.0.11-30 (s1-s20)
    'c': {'base': 50, 'max': 20},    # Compute: 10.0.0.51-70 (c1-c20)
    'm': {'base': 90, 'max': 20},    # MacOS: 10.0.0.71-90 (m1-m20)
}

# Dynamic IP allocation range for auto-assigned hostnames
DYNAMIC_IP_START = 200
DYNAMIC_IP_END = 249

# Core nodes use static IPs: s1=10.0.0.11, s2=10.0.0.12, s3=10.0.0.13
CORE_NODE_IPS = {
    's1': '10.0.0.11',
    's2': '10.0.0.12', 
    's3': '10.0.0.13'
}

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health monitoring"""
    
    def __init__(self, dhcp_server, *args, **kwargs):
        self.dhcp_server = dhcp_server
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        """Handle GET requests for health checks"""
        handlers = {
            '/health': self._health_data,
            '/status': self._status_data,
            '/leases': self._leases_data
        }
        
        if self.path in handlers:
            self._send_json_response(handlers[self.path]())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')
    
    def _health_data(self):
        """Get health check data"""
        etcd_healthy = self.dhcp_server.get_etcd_client() is not None
        
        if self.dhcp_server.running and etcd_healthy:
            return (200, {
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'etcd_connected': etcd_healthy,
                'server_ip': self.dhcp_server.server_ip
            })
        else:
            return (503, {
                'status': 'unhealthy',
                'timestamp': datetime.now().isoformat(),
                'etcd_connected': etcd_healthy,
                'dhcp_running': self.dhcp_server.running
            })
    
    def _status_data(self):
        """Get detailed status data"""
        return (200, {
            'status': 'running' if self.dhcp_server.running else 'stopped',
            'timestamp': datetime.now().isoformat(),
            'server_ip': self.dhcp_server.server_ip,
            'lease_count': len(self.dhcp_server.leases),
            'allocated_ips': len(self.dhcp_server.allocated_ips),
            'etcd_hosts': get_etcd_hosts(),
            'etcd_connected': self.dhcp_server.get_etcd_client() is not None
        })
    
    def _leases_data(self):
        """Get lease data"""
        leases_data = {
            mac: {
                'ip': lease['ip'],
                'hostname': lease.get('hostname', ''),
                'expires': lease['expires'],
                'allocated_at': lease.get('allocated_at', '')
            }
            for mac, lease in self.dhcp_server.leases.items()
        }
        
        return (200, {
            'leases': leases_data,
            'count': len(leases_data),
            'timestamp': datetime.now().isoformat()
        })
    
    def _send_json_response(self, data):
        """Send JSON response with error handling"""
        try:
            status_code, response_data = data
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = {
                'status': 'error',
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            }
            self.wfile.write(json.dumps(response).encode())
    
    def log_message(self, format, *args):
        """Override to use our logger"""
        logger.info(f"Health check: {format % args}")

class HealthServer:
    """HTTP server for health monitoring"""
    
    def __init__(self, dhcp_server, port=HEALTH_PORT):
        self.dhcp_server = dhcp_server
        self.port = port
        self.server = None
        self.thread = None
    
    def start(self):
        """Start the health monitoring server"""
        try:
            handler_class = lambda *args, **kwargs: HealthHandler(self.dhcp_server, *args, **kwargs)
            self.server = HTTPServer(('0.0.0.0', self.port), handler_class)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            logger.info(f"Health monitoring server started on port {self.port}")
        except Exception as e:
            logger.error(f"Failed to start health monitoring server: {e}")
    
    def stop(self):
        """Stop the health monitoring server"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            logger.info("Health monitoring server stopped")

class DHCPServer:
    def __init__(self):
        self.etcd_client = None
        self.leases = {}  # mac -> lease_info
        self.allocated_ips = set()
        self.server_ip = self.get_server_ip()
        self.running = False
        self.health_server = None
        
    def get_etcd_client(self):
        """Get etcd client with failover"""
        if self.etcd_client:
            try:
                self.etcd_client.status()
                return self.etcd_client
            except Exception:
                self.etcd_client = None

        try:
            self.etcd_client = get_etcd_client()
            logger.info("Connected to etcd")
            return self.etcd_client
        except Exception as e:
            logger.error(f"Could not connect to any etcd host: {e}")
            return None
    
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
        
        # Fallback: check hostname-based allocation
        hostname = socket.gethostname()
        
        # Check if this is a known core node
        if hostname in CORE_NODE_IPS:
            logger.info(f"Using core node IP for {hostname}: {CORE_NODE_IPS[hostname]}")
            return CORE_NODE_IPS[hostname]
        
        raise ValueError("Could not determine server IP from netplan primary interface or hostname")

    def determine_ip_from_hostname(self, hostname):
        """Generate deterministic IP based on hostname (same logic as Flask app)"""
        if not hostname:
            return None
        
        # Check if this is an AMT hostname (ends with 'a')
        is_amt = hostname.endswith('a')

        prefix = hostname[0]
        if is_amt:
            try:
                num = int(hostname[1:-1])
            except ValueError:
                return None
        else:
            try:
                num = int(hostname[1:])
            except ValueError:
                return None
        
        # Get IP range configuration for base node type
        if prefix not in IP_RANGES:
            return None
        
        config = IP_RANGES[prefix]
        
        # Validate number is within range
        if num < 1 or num > config['max']:
            return None
        
        # Calculate base IP address
        base_ip = config['base'] + num
        
        if is_amt:
            # AMT interface: use separate 10.10.10.0/24 subnet
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
        
        prefix = hostname[0].lower()
        if prefix == 's':
            return 'storage'
        elif prefix == 'c':
            return 'compute'
        elif prefix == 'm':
            return 'macos'
        else:
            return 'compute'
    
    def get_next_dynamic_ip(self):
        """Get the next available IP from the dynamic range 200-249"""
        client = self.get_etcd_client()

        # Get all allocated IPs from etcd nodes
        allocated_ips = set()
        for value, metadata in client.get_prefix(f"/cluster/nodes/by-hostname/"):
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
            'macos': 'm'
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
                    num = int(hostname[1:])
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
    
    @staticmethod
    def ip_to_int(ip):
        """Convert IP string to integer"""
        parts = ip.split('.')
        return (int(parts[0]) << 24) + (int(parts[1]) << 16) + (int(parts[2]) << 8) + int(parts[3])
    
    @staticmethod
    def int_to_ip(ip_int):
        """Convert integer to IP string"""
        return f"{(ip_int >> 24) & 0xFF}.{(ip_int >> 16) & 0xFF}.{(ip_int >> 8) & 0xFF}.{ip_int & 0xFF}"
    
    def allocate_hostname_and_ip(self, mac_address, requested_hostname=None):
        """Allocate hostname and IP using same logic as Flask app"""
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

        # Use requested hostname if provided and valid
        if requested_hostname:
            # Validate hostname format and calculate IP
            ip_address = self.determine_ip_from_hostname(requested_hostname)
            if ip_address:
                # Check if hostname is already taken by another MAC
                existing_hostname_data = client.get(f"/cluster/nodes/by-hostname/{requested_hostname}")
                if existing_hostname_data[0]:
                    existing_allocation = json.loads(existing_hostname_data[0].decode())
                    if existing_allocation['mac'] != normalized_mac:
                        logger.warning(f"Hostname {requested_hostname} already taken by {existing_allocation['mac']}")
                        # Fall back to auto-allocation
                        requested_hostname = None
                        hostname = None
                        ip_address = None
                    else:
                        # Same MAC, update allocation
                        hostname = requested_hostname
                        machine_type = self.determine_type_from_hostname(hostname)
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
        
        if not ip_address:
            logger.error(f"Could not determine IP for hostname {hostname}")
            return None, None
        
        # Create allocation record
        from datetime import datetime, timezone
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
            # If we're reallocating, remove old entries first
            if existing_data[0]:
                old_allocation = json.loads(existing_data[0].decode())
                old_hostname = old_allocation['hostname']
                if old_hostname != hostname:
                    client.delete(f"/cluster/nodes/by-hostname/{old_hostname}")
            
            # Use a transaction to ensure atomicity
            client.transaction(
                compare=[],  # No compare needed since we handle conflicts above
                success=[
                    client.transactions.put(f"/cluster/nodes/by-mac/{normalized_mac}", allocation_json),
                    client.transactions.put(f"/cluster/nodes/by-hostname/{hostname}", allocation_json)
                ],
                failure=[]
            )
            logger.info(f"Allocated {hostname} ({ip_address}) to {mac_address}")
            return hostname, ip_address
        except Exception as e:
            logger.error(f"Failed to store allocation in etcd: {e}")
            return None, None
    
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
    
    def handle_dhcp_discover(self, packet):
        """Handle DHCP DISCOVER packet"""
        mac = packet[Ether].src
        
        # Get requested IP and hostname if present
        requested_ip = None
        requested_hostname = None
        if packet.haslayer(DHCP):
            for option in packet[DHCP].options:
                if isinstance(option, tuple):
                    if option[0] == 'requested_addr':
                        requested_ip = option[1]
                    elif option[0] == 'hostname':
                        requested_hostname = option[1].decode() if isinstance(option[1], bytes) else option[1]
        
        # Log with client-provided details
        client_details = []
        if requested_hostname:
            client_details.append(f"hostname={requested_hostname}")
        if requested_ip:
            client_details.append(f"requested_ip={requested_ip}")
        client_info = f" ({', '.join(client_details)})" if client_details else ""
        logger.info(f"DHCP DISCOVER from {mac}{client_info}")
        
        offered_ip, hostname = self.get_or_create_lease(mac, requested_ip, requested_hostname)
        if not offered_ip:
            logger.error(f"No IP available for {mac}{client_info}")
            return
        
        # Create DHCP OFFER
        offer = self.create_dhcp_offer(packet, offered_ip, hostname)
        interface = self.get_interface_for_ip(self.server_ip)
        sendp(offer, iface=interface, verbose=0)
        logger.info(f"Sent DHCP OFFER {offered_ip} ({hostname}) to {mac}")
    
    def handle_dhcp_request(self, packet):
        """Handle DHCP REQUEST packet"""
        mac = packet[Ether].src
        
        # Get requested IP, hostname, and server ID
        requested_ip = None
        requested_hostname = None
        server_id = None
        if packet.haslayer(DHCP):
            for option in packet[DHCP].options:
                if isinstance(option, tuple):
                    if option[0] == 'requested_addr':
                        requested_ip = option[1]
                    elif option[0] == 'hostname':
                        requested_hostname = option[1].decode() if isinstance(option[1], bytes) else option[1]
                    elif option[0] == 'server_id':
                        server_id = option[1]
        
        # Log with client-provided details
        client_details = []
        if requested_hostname:
            client_details.append(f"hostname={requested_hostname}")
        if requested_ip:
            client_details.append(f"requested_ip={requested_ip}")
        client_info = f" ({', '.join(client_details)})" if client_details else ""
        logger.info(f"DHCP REQUEST from {mac}{client_info}")
        
        # Check if request is for us
        if server_id and server_id != self.server_ip:
            logger.info(f"DHCP REQUEST not for us (server_id: {server_id})")
            return
        
        # Check if we need to reallocate based on hostname
        should_reallocate = False
        if requested_hostname and mac in self.leases:
            # Ignore temporary hostnames during autoinstall
            if requested_hostname in ['ubuntu-server', 'localhost', '']:
                logger.info(f"Ignoring temporary hostname '{requested_hostname}' for {mac}")
                requested_hostname = None
                should_reallocate = False  # Don't reallocate for temporary hostnames
            else:
                current_hostname = self.leases[mac].get('hostname', '')
                expected_ip = self.determine_ip_from_hostname(requested_hostname)
                current_ip = self.leases[mac]['ip']
                
                if current_hostname != requested_hostname:
                    logger.info(f"Hostname changed from {current_hostname} to {requested_hostname} for {mac}")
                    should_reallocate = True
                elif expected_ip and expected_ip != current_ip:
                    logger.info(f"IP should change from {current_ip} to {expected_ip} for hostname {requested_hostname} on {mac}")
                    should_reallocate = True
        
        # Reallocate if needed
        if should_reallocate:
            # Remove old lease and create new one
            self.allocated_ips.discard(self.leases[mac]['ip'])
            del self.leases[mac]
            # Get new lease with requested hostname
            offered_ip, hostname = self.get_or_create_lease(mac, requested_ip, requested_hostname)
            if offered_ip:
                requested_ip = offered_ip
        
        # Validate the request
        if mac in self.leases and self.leases[mac]['ip'] == requested_ip:
            # Send ACK
            hostname = self.leases[mac].get('hostname', '')
            ack = self.create_dhcp_ack(packet, requested_ip, hostname)
            interface = self.get_interface_for_ip(self.server_ip)
            sendp(ack, iface=interface, verbose=0)
            logger.info(f"Sent DHCP ACK {requested_ip} ({hostname}) to {mac}")
        else:
            # Send NAK
            nak = self.create_dhcp_nak(packet)
            interface = self.get_interface_for_ip(self.server_ip)
            sendp(nak, iface=interface, verbose=0)
            logger.info(f"Sent DHCP NAK to {mac}")
    
    def get_interface_for_ip(self, ip):
        """Get the network interface for a given IP address"""
        import netifaces
        for interface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if addr['addr'] == ip:
                        return interface
        raise ValueError(f"No interface found for IP {ip}")

    def build_dhcp_options(self, message_type, hostname=''):
        """Build common DHCP options for OFFER and ACK packets"""
        options = [
            ('message-type', message_type),
            ('server_id', self.server_ip),
            ('lease_time', LEASE_TIME),
            ('subnet_mask', '255.255.255.0'),
            ('router', GATEWAY),
            ('name_server', DNS_SERVER),
            ('NTP_server', NTP_SERVER),
            ('tftp_server_name', self.server_ip),  # Option 66
            ('boot-file-name', 'EFI/BOOT/grubx64.efi')  # Option 67
        ]
        
        # Add hostname if available
        if hostname:
            options.insert(-1, ('hostname', hostname))
        
        options.append('end')
        return options

    def create_dhcp_offer(self, request_packet, offered_ip, hostname=''):
        """Create DHCP OFFER packet"""
        interface = self.get_interface_for_ip(self.server_ip)
        options = self.build_dhcp_options(2, hostname)  # OFFER = 2
        
        return (
            Ether(dst=request_packet[Ether].src, src=get_if_hwaddr(interface)) /
            IP(src=self.server_ip, dst='255.255.255.255') /
            UDP(sport=67, dport=68) /
            BOOTP(
                op=2,  # Boot reply
                htype=1,
                hlen=6,
                xid=request_packet[BOOTP].xid,
                yiaddr=offered_ip,
                siaddr=self.server_ip,
                chaddr=request_packet[BOOTP].chaddr
            ) /
            DHCP(options=options)
        )
    
    def create_dhcp_ack(self, request_packet, ack_ip, hostname=''):
        """Create DHCP ACK packet"""
        interface = self.get_interface_for_ip(self.server_ip)
        options = self.build_dhcp_options(5, hostname)  # ACK = 5
        
        return (
            Ether(dst=request_packet[Ether].src, src=get_if_hwaddr(interface)) /
            IP(src=self.server_ip, dst='255.255.255.255') /
            UDP(sport=67, dport=68) /
            BOOTP(
                op=2,  # Boot reply
                htype=1,
                hlen=6,
                xid=request_packet[BOOTP].xid,
                yiaddr=ack_ip,
                siaddr=self.server_ip,
                chaddr=request_packet[BOOTP].chaddr
            ) /
            DHCP(options=options)
        )
    
    def create_dhcp_nak(self, request_packet):
        """Create DHCP NAK packet"""
        interface = self.get_interface_for_ip(self.server_ip)
        return (
            Ether(dst=request_packet[Ether].src, src=get_if_hwaddr(interface)) /
            IP(src=self.server_ip, dst='255.255.255.255') /
            UDP(sport=67, dport=68) /
            BOOTP(
                op=2,  # Boot reply
                htype=1,
                hlen=6,
                xid=request_packet[BOOTP].xid,
                chaddr=request_packet[BOOTP].chaddr
            ) /
            DHCP(options=[
                ('message-type', 6),  # NAK
                ('server_id', self.server_ip),
                'end'
            ])
        )
    
    def dhcp_packet_handler(self, packet):
        """Handle incoming DHCP packets"""
        if not packet.haslayer(DHCP):
            return
        
        dhcp_message_type = None
        for option in packet[DHCP].options:
            if isinstance(option, tuple) and option[0] == 'message-type':
                dhcp_message_type = option[1]
                break
        
        if dhcp_message_type == 1:  # DISCOVER
            self.handle_dhcp_discover(packet)
        elif dhcp_message_type == 3:  # REQUEST
            self.handle_dhcp_request(packet)
    
    def cleanup_expired_leases(self):
        """Periodically clean up expired leases"""
        while self.running:
            try:
                now = datetime.now()
                expired_macs = []
                
                for mac, lease in self.leases.items():
                    # Handle legacy leases without expires field
                    if 'expires' not in lease:
                        logger.info(f"Legacy lease without expires field: {mac} -> {lease['ip']}")
                        # Convert to new format with current time + lease duration
                        expires = now + timedelta(seconds=LEASE_TIME)
                        lease['expires'] = expires.isoformat()
                        lease['hostname'] = lease.get('hostname', '')
                        lease['allocated_at'] = lease.get('allocated_at', now.isoformat())
                        # Save updated lease
                        self.save_lease_to_etcd(mac, lease)
                        continue
                    
                    expires = datetime.fromisoformat(lease['expires'])
                    if expires <= now:
                        expired_macs.append(mac)
                
                for mac in expired_macs:
                    lease = self.leases[mac]
                    self.allocated_ips.discard(lease['ip'])
                    del self.leases[mac]
                    logger.info(f"Expired lease: {mac} -> {lease['ip']}")
                
                time.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"Error in lease cleanup: {e}")
                time.sleep(60)
    
    def start(self):
        """Start the DHCP server"""
        logger.info("Starting DHCP server...")
        
        # Migrate existing leases to normalized MAC format
        self.migrate_leases_to_normalized_mac()
        
        # Load existing leases
        self.load_leases_from_etcd()
        
        # Start health monitoring server
        self.health_server = HealthServer(self)
        self.health_server.start()
        
        # Start cleanup thread
        self.running = True
        cleanup_thread = threading.Thread(target=self.cleanup_expired_leases, daemon=True)
        cleanup_thread.start()
        
        # Start packet sniffing
        interface = self.get_interface_for_ip(self.server_ip)
        logger.info(f"DHCP server listening on {interface} ({self.server_ip})")
        sniff(
            iface=interface,
            filter="udp and port 67",
            prn=self.dhcp_packet_handler,
            store=0
        )
    
    def stop(self):
        """Stop the DHCP server"""
        logger.info("Stopping DHCP server...")
        self.running = False
        if self.health_server:
            self.health_server.stop()

def main():
    if os.geteuid() != 0:
        print("This script must be run as root")
        sys.exit(1)

    # unset proxy environment variables, they can interfere with etcd connections
    os.environ.pop('http_proxy', None)
    os.environ.pop('https_proxy', None)
    server = DHCPServer()
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
        logger.info("DHCP server stopped")

if __name__ == '__main__':
    main()
