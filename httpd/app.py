import sqlite3
import json
from flask import Flask, request, jsonify
import os
import threading

app = Flask(__name__)

# Database path
DB_PATH = '/data/hostnames.db'

# Thread lock for database operations
db_lock = threading.Lock()

def determine_ip_from_hostname(hostname):
    """Generate deterministic IP based on hostname"""
    if hostname.startswith('s'):
        # Storage nodes: 10.0.0.10-29
        num = int(hostname[1:])
        return f"10.0.0.{10 + num}"  # s1 = .10, s2 = .11, etc.
    elif hostname.startswith('c'):
        # Compute nodes: 10.0.0.30-99
        num = int(hostname[1:])
        return f"10.0.0.{30 + num}"  # c1 = .30, c2 = .31, etc.
    elif hostname.startswith('m'):
        # MacOS nodes: 10.0.0.100-109
        num = int(hostname[1:])
        return f"10.0.0.{100 + num}"
    return None

def init_db():
    """Initialize the database with counters and allocations tables"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS counters (
            type TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS allocations (
            mac_address TEXT PRIMARY KEY,
            hostname TEXT NOT NULL,
            type TEXT NOT NULL,
            allocated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.close()

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

@app.route('/api/allocate')
def allocate_hostname():
    """Allocate a new hostname based on MAC address"""
    mac_address = request.args.get('mac')
    
    if not mac_address:
        return jsonify({'error': 'MAC address is required'}), 400
    
    # Normalize MAC address
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')
    
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        
        # Check if this MAC address already has an allocation
        cursor = conn.execute('SELECT hostname, type FROM allocations WHERE mac_address = ?', (normalized_mac,))
        existing = cursor.fetchone()
        
        if existing:
            hostname, machine_type = existing
            # Compute IP deterministically from hostname
            ip_address = determine_ip_from_hostname(hostname)
            
            conn.close()
            return jsonify({'hostname': hostname, 'type': machine_type, 'ip': ip_address, 'mac': mac_address, 'existing': True})
        
        # Determine machine type from MAC address
        machine_type = determine_type_from_mac(mac_address)
        
        # Map types to prefixes
        prefixes = {
            'storage': 's',
            'macos': 'm',
            'compute': 'c'
        }
        
        prefix = prefixes.get(machine_type, 'c')
        
        # Insert or ignore the type
        conn.execute('INSERT OR IGNORE INTO counters (type, count) VALUES (?, 0)', (machine_type,))
        
        # Increment counter
        conn.execute('UPDATE counters SET count = count + 1 WHERE type = ?', (machine_type,))
        
        # Get the new count
        cursor = conn.execute('SELECT count FROM counters WHERE type = ?', (machine_type,))
        count = cursor.fetchone()[0]
        
        hostname = f"{prefix}{count}"
        
        # Generate deterministic IP
        ip_address = determine_ip_from_hostname(hostname)
        
        # Store the allocation
        conn.execute('INSERT INTO allocations (mac_address, hostname, type) VALUES (?, ?, ?)', 
                    (normalized_mac, hostname, machine_type))
        
        conn.commit()
        conn.close()
    
    return jsonify({'hostname': hostname, 'type': machine_type, 'ip': ip_address, 'mac': mac_address, 'existing': False})

@app.route('/api/status')
def status():
    """Get current status of all counters"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute('SELECT type, count FROM counters')
    counters = dict(cursor.fetchall())
    conn.close()
    return jsonify(counters)

def generate_dhcp_config():
    """Generate static DHCP configuration for dnsmasq"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute('SELECT mac_address, hostname FROM allocations ORDER BY hostname')
        allocations = cursor.fetchall()
        conn.close()
    
    # Generate dhcp-host entries
    dhcp_config = []
    for mac, hostname in allocations:
        # Convert normalized MAC back to colon format
        mac_formatted = ':'.join(mac[i:i+2] for i in range(0, 12, 2))
        # Compute IP deterministically from hostname
        ip = determine_ip_from_hostname(hostname)
        if ip:
            dhcp_config.append(f"dhcp-host={mac_formatted},{hostname},{ip},infinite")
    
    return '\n'.join(dhcp_config) + '\n' if dhcp_config else ''

@app.route('/api/allocations')
def allocations():
    """Get all current allocations"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute('SELECT mac_address, hostname, type, allocated_at FROM allocations ORDER BY allocated_at')
    allocations = []
    for row in cursor.fetchall():
        mac, hostname, machine_type, allocated_at = row
        # Compute IP deterministically from hostname
        ip = determine_ip_from_hostname(hostname)
        allocations.append({
            'mac': mac,
            'hostname': hostname,
            'type': machine_type,
            'ip': ip,
            'allocated_at': allocated_at
        })
    conn.close()
    return jsonify(allocations)

@app.route('/api/dhcp-config')
def get_dhcp_config():
    """Get current DHCP configuration"""
    config = generate_dhcp_config()
    if config:
        return config, 200, {'Content-Type': 'text/plain'}
    else:
        return "No static hosts configured yet", 404

if __name__ == '__main__':
    init_db()
    app.run(host='127.0.0.1', port=12723)
