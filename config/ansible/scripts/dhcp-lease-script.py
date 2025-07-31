#!/usr/bin/env python3
"""
DHCP lease script for dnsmasq integration with etcd
Called by dnsmasq for lease changes
Arguments: action mac ip hostname
Special action 'restore' reads all leases from etcd and writes to dnsmasq.leases file
"""

import sys
import os
import json
import time
import etcd3

def get_etcd_client():
    """Get etcd client with configuration from environment"""
    etcd_hosts = os.environ.get('ETCD_HOSTS', 'localhost:2379')
    
    # Parse etcd host and port
    host_port = etcd_hosts.split(',')[0]  # Use first host
    if ':' in host_port:
        host, port = host_port.split(':')
        port = int(port)
    else:
        host = host_port
        port = 2379
    
    return etcd3.client(host=host, port=port)

def restore_leases():
    """Restore DHCP leases from etcd to dnsmasq.leases file"""
    etcd_prefix = "/cluster/dhcp/leases"
    leases_file = "/var/lib/dhcp/dnsmasq.leases"
    
    try:
        client = get_etcd_client()
        
        # Create leases directory if it doesn't exist
        os.makedirs("/var/lib/dhcp", exist_ok=True)
        
        # Get all lease data from etcd
        with open(leases_file, 'w') as f:
            for value, metadata in client.get_prefix(etcd_prefix):
                if value:
                    try:
                        lease_data = json.loads(value.decode('utf-8'))
                        mac = lease_data.get('mac', '')
                        ip = lease_data.get('ip', '')
                        hostname = lease_data.get('hostname', '*')
                        timestamp = lease_data.get('timestamp', 0)
                        
                        if mac and ip:
                            # Write in dnsmasq lease format: timestamp mac ip hostname client-id
                            f.write(f"{timestamp} {mac} {ip} {hostname} *\n")
                    except (json.JSONDecodeError, KeyError) as e:
                        print(f"Error parsing lease data: {e}", file=sys.stderr)
                        continue
        
        print("DHCP leases restored from etcd")
        
    except Exception as e:
        print(f"Error restoring leases from etcd: {e}", file=sys.stderr)
        sys.exit(1)

def handle_lease_change(action, mac, ip, hostname):
    """Handle DHCP lease changes (add/old/del)"""
    etcd_prefix = "/cluster/dhcp/leases"
    
    try:
        client = get_etcd_client()
        
        if action in ['add', 'old']:
            # Store lease in etcd
            lease_data = {
                'mac': mac,
                'ip': ip,
                'hostname': hostname,
                'action': action,
                'timestamp': int(time.time())
            }
            
            key = f"{etcd_prefix}/{mac}"
            value = json.dumps(lease_data)
            client.put(key, value)
            
        elif action == 'del':
            # Remove lease from etcd
            key = f"{etcd_prefix}/{mac}"
            client.delete(key)
            
    except Exception as e:
        print(f"Error updating etcd: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    # Debug: log actual arguments received
    try:
        with open('/tmp/dhcp-script-debug.log', 'a') as f:
            f.write(f"Args received: {sys.argv}\n")
            f.write(f"Arg count: {len(sys.argv)}\n")
    except Exception as e:
        print(f"Debug logging failed: {e}", file=sys.stderr)
    
    if len(sys.argv) < 2:
        print("Usage: dhcp-lease-script.py action [mac ip [hostname]]", file=sys.stderr)
        print("  action: add|old|del|restore", file=sys.stderr)
        print("  For restore action, only action argument is needed", file=sys.stderr)
        sys.exit(1)
    
    action = sys.argv[1]
    
    if action == 'restore':
        restore_leases()
    elif len(sys.argv) >= 4:
        mac = sys.argv[2]
        ip = sys.argv[3]
        hostname = sys.argv[4] if len(sys.argv) > 4 else '*'
        handle_lease_change(action, mac, ip, hostname)
    else:
        print("Usage: dhcp-lease-script.py action mac ip [hostname]", file=sys.stderr)
        print("  or: dhcp-lease-script.py restore", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
