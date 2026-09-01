#!/usr/bin/env python3
"""
Scapy-based DHCP server with etcd integration for YCluster
"""

import os
import sys
import time
import json
import threading
import logging
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from scapy.all import sniff, sendp
from scapy.layers.dhcp import DHCP, BOOTP
from scapy.layers.l2 import Ether

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from ..common.etcd_utils import get_etcd_client, get_etcd_hosts
from .dhcp_packets import (LEASE_TIME, create_dhcp_ack, create_dhcp_nak,
                           create_dhcp_offer, interface_for_ip)
from .dhcp_allocation import DHCPAllocationMixin
from .dhcp_leases import DHCPLeaseStore

# Configuration
HEALTH_PORT = int(os.environ.get('DHCP_HEALTH_PORT', '8067'))

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
        if self.path == '/metrics':
            self._send_metrics_response()
        elif self.path == '/health':
            self._send_json_response(self._health_data())
        elif self.path == '/status':
            self._send_json_response(self._status_data())
        elif self.path == '/leases':
            self._send_json_response(self._leases_data())
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
    
    def _send_metrics_response(self):
        """Send Prometheus metrics response"""
        try:
            metrics = self._generate_prometheus_metrics()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            self.wfile.write(metrics.encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            error_metrics = f'# Error generating metrics: {str(e)}\n'
            self.wfile.write(error_metrics.encode('utf-8'))
    
    def _generate_prometheus_metrics(self):
        """Generate Prometheus format metrics"""
        etcd_healthy = self.dhcp_server.get_etcd_client() is not None
        server_running = self.dhcp_server.running
        
        metrics = []
        
        # DHCP server status
        metrics.append('# HELP dhcp_server_up DHCP server running status')
        metrics.append('# TYPE dhcp_server_up gauge')
        metrics.append(f'dhcp_server_up{{server_ip="{self.dhcp_server.server_ip}"}} {1 if server_running else 0}')
        
        # etcd connection status
        metrics.append('# HELP dhcp_server_etcd_connected DHCP server etcd connection status')
        metrics.append('# TYPE dhcp_server_etcd_connected gauge')
        metrics.append(f'dhcp_server_etcd_connected{{server_ip="{self.dhcp_server.server_ip}"}} {1 if etcd_healthy else 0}')
        
        # Lease statistics
        metrics.append('# HELP dhcp_server_leases_total Total number of DHCP leases')
        metrics.append('# TYPE dhcp_server_leases_total gauge')
        metrics.append(f'dhcp_server_leases_total{{server_ip="{self.dhcp_server.server_ip}"}} {len(self.dhcp_server.leases)}')
        
        # Allocated IPs
        metrics.append('# HELP dhcp_server_allocated_ips_total Total number of allocated IP addresses')
        metrics.append('# TYPE dhcp_server_allocated_ips_total gauge')
        metrics.append(f'dhcp_server_allocated_ips_total{{server_ip="{self.dhcp_server.server_ip}"}} {len(self.dhcp_server.allocated_ips)}')
        
        # Overall health status (for compatibility with existing alerts)
        overall_healthy = server_running and etcd_healthy
        metrics.append('# HELP dhcp_server_healthy Overall DHCP server health status')
        metrics.append('# TYPE dhcp_server_healthy gauge')
        metrics.append(f'dhcp_server_healthy{{server_ip="{self.dhcp_server.server_ip}"}} {1 if overall_healthy else 0}')
        
        return '\n'.join(metrics) + '\n'
    
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
        logger.debug(f"Health check: {format % args}")

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

class DHCPServer(DHCPAllocationMixin, DHCPLeaseStore):
    def __init__(self):
        self.leases = {}  # mac -> lease_info
        self.allocated_ips = set()
        self.server_ip = self.get_server_ip()
        self.running = False
        self.health_server = None

    def get_etcd_client(self):
        """Get etcd client with failover"""
        try:
            client = get_etcd_client()
            logger.debug("Connected to etcd")
            return client
        except Exception as e:
            logger.error(f"Could not connect to any etcd host: {e}")
            return None
    
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
        assert hostname is not None

        # Create DHCP OFFER
        offer = create_dhcp_offer(self.server_ip, packet, offered_ip, hostname)
        interface = interface_for_ip(self.server_ip)
        sendp(offer, iface=interface, verbose=0)
        logger.info(f"Sent DHCP OFFER {offered_ip} ({hostname}) to {mac}")
    
    def handle_dhcp_request(self, packet):
        """Handle DHCP REQUEST packet"""
        mac = packet[Ether].src
        requested_ip, requested_hostname, server_id = self._request_details(packet)
        client_info = self._client_details(requested_ip, requested_hostname)
        logger.info(f"DHCP REQUEST from {mac}{client_info}")

        # Check if request is for us
        if server_id and server_id != self.server_ip:
            logger.info(f"DHCP REQUEST not for us (server_id: {server_id})")
            return

        should_reallocate = self._request_should_reallocate(mac, requested_hostname)

        # Reallocate if needed
        if should_reallocate:
            requested_ip = self._reallocate_lease(
                mac, requested_ip, requested_hostname)

        # Validate the request
        if mac in self.leases and self.leases[mac]['ip'] == requested_ip:
            # Send ACK
            hostname = self.leases[mac].get('hostname', '')
            ack = create_dhcp_ack(self.server_ip, packet, requested_ip, hostname)
            self._send_dhcp_reply(packet, ack)
            logger.info(f"Sent DHCP ACK {requested_ip} ({hostname}) to {mac}")
        else:
            # Send NAK
            nak = create_dhcp_nak(self.server_ip, packet)
            self._send_dhcp_reply(packet, nak)
            reason = self._nak_reason(mac, requested_ip)
            logger.info(f"Sent DHCP NAK to {mac}: {reason}")

    def _request_details(self, packet):
        """Read requested IP, hostname, and server ID from a DHCP packet."""
        requested_ip = None
        requested_hostname = None
        server_id = None
        if packet.haslayer(DHCP):
            for option in packet[DHCP].options:
                if not isinstance(option, tuple):
                    continue
                if option[0] == 'requested_addr':
                    requested_ip = option[1]
                elif option[0] == 'hostname':
                    value = option[1]
                    requested_hostname = value.decode() if isinstance(value, bytes) else value
                elif option[0] == 'server_id':
                    server_id = option[1]

        # If no requested_addr option, fall back to ciaddr (used in renewals
        # per RFC 2131)
        if not requested_ip and packet.haslayer(BOOTP):
            ciaddr = packet[BOOTP].ciaddr
            if ciaddr and ciaddr != '0.0.0.0':
                requested_ip = ciaddr
        return requested_ip, requested_hostname, server_id

    @staticmethod
    def _client_details(requested_ip, requested_hostname):
        client_details = []
        if requested_hostname:
            client_details.append(f"hostname={requested_hostname}")
        if requested_ip:
            client_details.append(f"requested_ip={requested_ip}")
        return f" ({', '.join(client_details)})" if client_details else ""

    def _request_should_reallocate(self, mac, requested_hostname):
        """Whether an existing lease must be replaced by a hostname request."""
        if not requested_hostname or mac not in self.leases:
            return False

        # Ignore temporary hostnames during autoinstall
        if requested_hostname in ['ubuntu-server', 'localhost', '']:
            logger.info(f"Ignoring temporary hostname '{requested_hostname}' for {mac}")
            return False

        lease = self.leases[mac]
        current_hostname = lease.get('hostname', '')
        expected_ip = self.determine_ip_from_hostname(requested_hostname)
        current_ip = lease['ip']

        if current_hostname != requested_hostname:
            logger.info(f"Hostname changed from {current_hostname} to "
                        f"{requested_hostname} for {mac}")
            return True
        if expected_ip and expected_ip != current_ip:
            logger.info(f"IP should change from {current_ip} to {expected_ip} "
                        f"for hostname {requested_hostname} on {mac}")
            return True
        return False

    def _reallocate_lease(self, mac, requested_ip, requested_hostname):
        """Remove the old lease and create a replacement, returning its IP."""
        self.allocated_ips.discard(self.leases[mac]['ip'])
        del self.leases[mac]
        offered_ip, _ = self.get_or_create_lease(
            mac, requested_ip, requested_hostname)
        return offered_ip if offered_ip else requested_ip

    def _send_dhcp_reply(self, packet, reply):
        interface = interface_for_ip(self.server_ip)
        sendp(reply, iface=interface, verbose=0)

    def _nak_reason(self, mac, requested_ip):
        if mac not in self.leases:
            return "no lease found"
        if requested_ip is None:
            return "no requested IP in options or ciaddr"
        return (f"lease IP {self.leases[mac]['ip']} != "
                f"requested {requested_ip}")
    
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
        interface = interface_for_ip(self.server_ip)
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
