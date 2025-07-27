import json
import etcd3
from flask import Flask, request, jsonify
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)

# etcd configuration
ETCD_HOSTS = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')
ETCD_PREFIX = '/cluster/nodes'

# Thread lock for allocation operations
allocation_lock = threading.Lock()

# Global etcd client
etcd_client = None

def get_etcd_client():
    """Get or create etcd client with failover support"""
    global etcd_client
    
    if etcd_client:
        try:
            # Test if connection is alive
            etcd_client.status()
            return etcd_client
        except:
            etcd_client = None
    
    # Try each host in order
    for host_port in ETCD_HOSTS:
        try:
            host, port = host_port.split(':')
            client = etcd3.client(host=host, port=int(port))
            # Test connection
            client.status()
            etcd_client = client
            return client
        except:
            continue
    
    raise Exception("Could not connect to any etcd host")

def determine_ip_from_hostname(hostname):
    """Generate deterministic IP based on hostname"""
    if hostname.startswith('s'):
        num = int(hostname[1:])
        return f"10.0.0.{10 + num}"  # s1 = .11, s2 = .12, etc.
    elif hostname.startswith('c'):
        num = int(hostname[1:])
        return f"10.0.0.{30 + num}"  # c1 = .31, c2 = .32, etc.
    elif hostname.startswith('m'):
        num = int(hostname[1:])
        return f"10.0.0.{50 + num}"
    return None

def determine_type_from_mac(mac_address):
    """Determine machine type based on MAC address prefix"""
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
        'macos': 'm'
    }
    
    prefix = prefixes.get(node_type, 'c')
    
    # Get all existing hostnames of this type
    existing_numbers = []
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/{prefix}"):
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

@app.route('/api/allocate')
def allocate_hostname():
    """Allocate a new hostname based on MAC address"""
    mac_address = request.args.get('mac')
    
    if not mac_address:
        return jsonify({'error': 'MAC address is required'}), 400
    
    # Normalize MAC address
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')
    
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {str(e)}'}), 503
    
    with allocation_lock:
        # Check if this MAC address already has an allocation
        existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
        if existing_data[0]:
            allocation = json.loads(existing_data[0].decode())
            return jsonify({
                'hostname': allocation['hostname'],
                'type': allocation['type'],
                'ip': allocation['ip'],
                'mac': mac_address,
                'existing': True
            })
        
        # Determine machine type from MAC address
        machine_type = determine_type_from_mac(mac_address)
        
        # Get next available hostname
        hostname = get_next_hostname(client, machine_type)
        
        # Generate deterministic IP
        ip_address = determine_ip_from_hostname(hostname)
        
        # Create allocation record
        allocation = {
            'hostname': hostname,
            'type': machine_type,
            'ip': ip_address,
            'mac': normalized_mac,
            'allocated_at': datetime.now(datetime.UTC).isoformat()
        }
        
        # Store in etcd with both lookups
        allocation_json = json.dumps(allocation)
        
        # Use a transaction to ensure atomicity
        client.transaction(
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
        
        return jsonify({
            'hostname': hostname,
            'type': machine_type,
            'ip': ip_address,
            'mac': mac_address,
            'existing': False
        })

@app.route('/api/status')
def status():
    """Get current allocation counts by type"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {str(e)}'}), 503
    
    counts = {'storage': 0, 'compute': 0, 'macos': 0}
    
    # Count allocations by type
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                node_type = allocation.get('type', 'compute')
                counts[node_type] = counts.get(node_type, 0) + 1
            except:
                pass
    
    return jsonify(counts)

@app.route('/api/allocations')
def allocations():
    """Get all current allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return jsonify({'error': f'etcd connection failed: {str(e)}'}), 503
    
    allocations = []
    
    # Get all allocations from by-hostname (to avoid duplicates)
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                allocations.append({
                    'mac': allocation['mac'],
                    'hostname': allocation['hostname'],
                    'type': allocation['type'],
                    'ip': allocation['ip'],
                    'allocated_at': allocation.get('allocated_at', 0)
                })
            except:
                pass
    
    # Sort by hostname
    allocations.sort(key=lambda x: (x['type'], int(x['hostname'][1:]) if x['hostname'][1:].isdigit() else 0))
    
    return jsonify(allocations)

@app.route('/api/dhcp-config')
def get_dhcp_config():
    """Generate DHCP configuration from etcd allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return f"# etcd connection failed: {str(e)}\n", 503
    
    dhcp_config = []
    
    # Get all allocations
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                mac = allocation['mac']
                hostname = allocation['hostname']
                ip = allocation['ip']
                
                # Convert normalized MAC back to colon format
                mac_formatted = ':'.join(mac[i:i+2] for i in range(0, 12, 2))
                dhcp_config.append(f"dhcp-host={mac_formatted},{hostname},{ip},infinite")
            except:
                pass
    
    if dhcp_config:
        return '\n'.join(sorted(dhcp_config)) + '\n', 200, {'Content-Type': 'text/plain'}
    else:
        return "# No static hosts configured yet\n", 200, {'Content-Type': 'text/plain'}

@app.route('/api/health')
def health():
    """Health check endpoint"""
    try:
        client = get_etcd_client()
        # Try to read a key to verify connection
        client.get('/test')
        return jsonify({'status': 'healthy', 'etcd': 'connected'})
    except:
        return jsonify({'status': 'unhealthy', 'etcd': 'disconnected'}), 503

if __name__ == '__main__':
    # Wait for etcd to be available
    while True:
        try:
            client = get_etcd_client()
            print("Connected to etcd successfully")
            break
        except Exception as e:
            print(f"Waiting for etcd: {e}")
            time.sleep(5)
    
    app.run(host='0.0.0.0', port=12723)
