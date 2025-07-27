#!/usr/bin/python3
"""
Populate etcd with the local node's entry for recovery purposes.
This runs on each node and registers itself in etcd.
"""

import etcd3
import json
import sys
import os
import socket
import subprocess
from datetime import datetime, UTC

# Get initial etcd host from environment or use localhost
INITIAL_ETCD_HOST = os.environ.get('ETCD_HOST', 'localhost:2379')
ETCD_PREFIX = '/cluster/nodes'

def get_etcd_client():
    """Create etcd client, trying local first then discovering cluster members"""
    # First try localhost (if etcd is running locally)
    try:
        client = etcd3.client(host='localhost', port=2379)
        client.status()
        return client
    except:
        pass
    
    # Try the initial host
    try:
        host, port = INITIAL_ETCD_HOST.split(':')
        client = etcd3.client(host=host, port=int(port))
        client.status()
        return client
    except:
        pass
    
    # Try to discover cluster members from initial host
    try:
        host, port = INITIAL_ETCD_HOST.split(':')
        client = etcd3.client(host=host, port=int(port))
        
        for member in client.members:
            for url in member.client_urls:
                if url.startswith('http://'):
                    member_host = url.replace('http://', '').split(':')[0]
                    try:
                        test_client = etcd3.client(host=member_host, port=2379)
                        test_client.status()
                        return test_client
                    except:
                        continue
    except:
        pass
    
    raise Exception("Could not connect to any etcd host")

def get_local_info():
    """Get local node information"""
    # Get hostname
    hostname = socket.gethostname()
    
    # Get primary network interface and MAC
    try:
        # Get the default interface
        result = subprocess.run(['ip', 'route', 'show', 'default'], 
                              capture_output=True, text=True)
        default_iface = result.stdout.split()[4]
        
        # Get MAC address
        with open(f'/sys/class/net/{default_iface}/address', 'r') as f:
            mac = f.read().strip()
        
        # Get IP address
        result = subprocess.run(['ip', '-4', 'addr', 'show', default_iface], 
                              capture_output=True, text=True)
        for line in result.stdout.split('\n'):
            if 'inet ' in line:
                ip = line.split()[1].split('/')[0]
                break
    except Exception as e:
        print(f"Error getting network info: {e}")
        sys.exit(1)
    
    # Determine node type from hostname
    if hostname.startswith('s'):
        node_type = 'storage'
    elif hostname.startswith('c'):
        node_type = 'compute'
    else:
        node_type = 'unknown'
    
    return {
        'hostname': hostname,
        'mac': mac.lower().replace(':', ''),  # Normalize MAC address
        'ip': ip,
        'type': node_type,
        'allocated_at': datetime.now(UTC).isoformat()
    }

def populate_local_node():
    """Populate etcd with local node entry"""
    try:
        client = get_etcd_client()
        node_info = get_local_info()
        
        # Node key
        key = f"{ETCD_PREFIX}/by-hostname/{node_info['hostname']}"
        value = json.dumps(node_info)
        
        # Check if entry already exists
        existing = client.get(key)
        if existing[0] is not None:
            print(f"Node {node_info['hostname']} already exists in etcd")
            existing_data = json.loads(existing[0])
            print(f"Existing entry: {json.dumps(existing_data, indent=2)}")
        else:
            # Put the node data
            client.put(key, value)
            print(f"Added node {node_info['hostname']} to etcd")
        
        # Always update the MAC to hostname mapping
        mac_key = f"{ETCD_PREFIX}/by-mac/{node_info['mac']}"
        client.put(mac_key, json.dumps(node_info))
        print(f"Updated MAC mapping for {node_info['mac']} -> {node_info['hostname']}")
        
        print(f"\nNode info: {json.dumps(node_info, indent=2)}")
        
    except Exception as e:
        print(f"Error populating node: {e}")
        sys.exit(1)

if __name__ == '__main__':
    populate_local_node()
