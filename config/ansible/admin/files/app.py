"""
XCluster Admin API Service

etcd Schema Documentation:
==========================

Node Allocations:
- /cluster/nodes/by-mac/{normalized_mac} -> allocation JSON
  * normalized_mac: MAC address with colons/dashes removed, lowercase (e.g., "5847caabcdef")
  * allocation JSON contains: hostname, type, ip, amt_ip, mac (normalized), allocated_at

- /cluster/nodes/by-hostname/{normalized_mac} -> allocation JSON (same as above)
  * hostname: node hostname like "s1", "c5", "m3"

DHCP Leases:
- /cluster/dhcp/leases/{lease_key} -> lease JSON
  * lease JSON contains: ip, mac (non-normalized with colons, e.g., "58:47:ca:ab:cd:ef")

Leadership:
- /cluster/leader/app -> hostname of current storage leader
- /cluster/leader/dhcp -> hostname of current DHCP leader

Node Management:
- /cluster/nodes/{hostname}/drain -> "true" if node is drained

TLS Configuration:
- /cluster/tls/cert -> PEM certificate data
- /cluster/tls/key -> PEM private key data

MAC Address Formats:
- Normalized: lowercase, no separators (5847caabcdef) - used in etcd keys
- Non-normalized: with colons (58:47:ca:ab:cd:ef) - used in DHCP leases and network tools
"""

import json
import sys

import etcd3
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file
import ntplib
import os
import threading
import time
import subprocess
import socket
import platform
import requests
from datetime import datetime, UTC
import dns.resolver
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from jinja2 import Template

AUTOINSTALL_USER_DATA_TEMPLATE = os.path.join(os.path.dirname(__file__), 'templates', 'user-data.j2')

app = Flask(__name__)

# Node type interface configurations
NODE_TYPE_INTERFACES = {
    'storage': {
        'cluster_interface': 'enp2s0f0np0',
        'uplink_interface': 'enp87s0',
        'amt_interface': 'enp89s0'
    },
    'compute': {
        'cluster_interface': 'enp1s0f0',
        'uplink_interface': 'enp1s0f1',
        'amt_interface': 'enp1s0f2'
    },
    'macos': {
        'cluster_interface': 'en0',
        'uplink_interface': 'en1',
        'amt_interface': 'en2'
    }
}

# etcd configuration
ETCD_HOSTS = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')
ETCD_PREFIX = '/cluster/nodes'

# Core nodes configuration
CORE_NODES = ['s1', 's2', 's3']

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

# IP allocation configuration (avoiding DHCP range 10.0.0.100-200)
IP_RANGES = {
    's': {'base': 10, 'max': 20},    # Storage: 10.0.0.11-30 (s1-s20)
    'c': {'base': 50, 'max': 20},    # Compute: 10.0.0.51-70 (c1-c20)
    'm': {'base': 90, 'max': 20},    # MacOS: 10.0.0.71-90 (m1-m20)
}

def determine_ip_from_hostname(hostname):
    """Generate deterministic IP based on hostname"""
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
        raise ValueError(f"Node number {num} for prefix '{prefix}' exceeds range 1-{config['max']}")
    
    # Calculate base IP address
    base_ip = config['base'] + num
    
    if is_amt:
        # AMT interface: use separate 10.10.10.0/24 subnet
        return f"10.10.10.{base_ip}"
    else:
        # Regular interface
        return f"10.0.0.{base_ip}"

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

def get_or_create_allocation(mac_address):
    """Get existing allocation or create new one for non-normalized MAC address"""
    client = get_etcd_client()
    normalized_mac = mac_address.lower().replace(':', '').replace('-', '')
    
    # Check if allocation already exists
    existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
    if existing_data[0]:
        data = json.loads(existing_data[0].decode())
        # Some old entries may lack IP or amt_ip, fill them in
        if 'amt_ip' not in data:
            amt_ip_address = determine_ip_from_hostname(data['hostname'] + 'a')
            data['amt_ip'] = amt_ip_address
        return data

    
    # Create new allocation
    with allocation_lock:
        # Double-check after acquiring lock
        existing_data = client.get(f"{ETCD_PREFIX}/by-mac/{normalized_mac}")
        if existing_data[0]:
            return json.loads(existing_data[0].decode())
        
        # Determine machine type and allocate hostname
        machine_type = determine_type_from_mac(mac_address)
        hostname = get_next_hostname(client, machine_type)
        ip_address = determine_ip_from_hostname(hostname)
        amt_ip_address = determine_ip_from_hostname(hostname + "a")
        
        allocation_data = {
            'hostname': hostname,
            'type': machine_type,
            'ip': ip_address,
            'amt_ip': amt_ip_address,
            'mac': normalized_mac,
            'allocated_at': datetime.now(UTC).isoformat()
        }
        
        # Store in etcd
        allocation_json = json.dumps(allocation_data)
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
        
        return allocation_data

@app.route('/api/allocate')
def allocate_hostname():
    """Allocate a new hostname based on MAC address"""
    mac_address = request.args.get('mac')
    
    if not mac_address:
        return jsonify({'error': 'MAC address is required'}), 400
    
    try:
        allocation = get_or_create_allocation(mac_address)
        
        return jsonify({
            'hostname': allocation['hostname'],
            'type': allocation['type'],
            'ip': allocation['ip'],
            'amt_ip': allocation['amt_ip'],
            'mac': mac_address,
            'existing': True  # Always true since we return existing or newly created
        })
    except Exception as e:
        return jsonify({'error': f'Allocation failed: {str(e)}'}), 500

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

@app.route('/api/hosts')
def get_hosts():
    """Generate hosts file format from etcd allocations"""
    try:
        client = get_etcd_client()
    except Exception as e:
        return f"# etcd connection failed: {str(e)}\n", 503
    
    hosts_entries = []
    
    # Get all allocations
    for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                hostname = allocation['hostname']
                ip = allocation['ip']
                
                # Add main hostname entry
                hosts_entries.append(f"{ip} {hostname} {hostname}.xc")
                
                # Add AMT hostname entry if this is a regular node (not already AMT)
                if not hostname.endswith('a'):
                    amt_hostname = f"{hostname}a"
                    amt_ip = determine_ip_from_hostname(amt_hostname)
                    if amt_ip:
                        hosts_entries.append(f"{amt_ip} {amt_hostname} {amt_hostname}.xc")
            except:
                pass
    
    # Add service aliases that point to storage VIP
    hosts_entries.append("10.0.0.100 registry.xc")

    if hosts_entries:
        return '\n'.join(sorted(hosts_entries)) + '\n', 200, {'Content-Type': 'text/plain'}
    else:
        return "# No static hosts configured yet\n", 200, {'Content-Type': 'text/plain'}

def check_service_status(service_name):
    """Check if a systemd service is active"""
    try:
        result = subprocess.run(['systemctl', 'is-active', service_name], 
                              capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == 'active'
    except:
        return False

def check_port_open(host, port, timeout=3):
    """Check if a port is open on a host"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def check_ceph_status():
    """Check Ceph cluster health"""
    try:
        result = subprocess.run(['ceph', 'health'], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            health_output = result.stdout.strip()
            return {
                'status': 'healthy' if 'HEALTH_OK' in health_output else 'degraded',
                'details': health_output
            }
        else:
            return {'status': 'error', 'details': result.stderr.strip()}
    except:
        return {'status': 'unavailable', 'details': 'ceph command failed'}

def check_dns_status():
    """Check DNS (dnsmasq) service and functionality"""
    try:
        # Check if dnsmasq service is running
        service_running = check_service_status('dnsmasq')
        
        # Test local DNS server directly using dnspython
        dns_working = False
        dns_details = "DNS query failed"
        
        try:
            # Create a resolver that queries the local DNS server directly
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ['127.0.0.1']
            resolver.timeout = 3
            resolver.lifetime = 5
            
            # Query local hostname A record
            local_hostname = platform.node()
            answer = resolver.resolve(local_hostname, 'A')
            if answer:
                resolved_ips = [str(rdata) for rdata in answer]
                dns_working = True
                dns_details = f"Local DNS server responding ({local_hostname} -> {', '.join(resolved_ips)})"
            else:
                dns_details = f"Local DNS query for {local_hostname} returned no results"
                
        except dns.resolver.Timeout:
            dns_details = "Local DNS query timeout"
        except dns.resolver.NXDOMAIN:
            dns_details = "Local DNS query: domain not found"
        except dns.resolver.NoAnswer:
            dns_details = "Local DNS query: no answer"
        except Exception as e:
            dns_details = f"Local DNS query error: {str(e)}"
        
        # Overall status
        if service_running and dns_working:
            status = 'healthy'
            details = f"Service active, {dns_details}"
        elif service_running:
            status = 'degraded'
            details = f"Service active but {dns_details}"
        else:
            status = 'unhealthy'
            details = f"Service inactive, {dns_details}"
            
        return {
            'status': status,
            'details': {
                'service_active': service_running,
                'dns_working': dns_working,
                'message': details
            }
        }
    except Exception as e:
        return {'status': 'error', 'details': f'DNS check failed: {str(e)}'}

def check_certificate_expiry():
    """Check TLS certificate expiry from etcd"""
    try:
        client = get_etcd_client()
        cert_value, _ = client.get('/cluster/tls/cert')
        
        if not cert_value:
            return {
                'status': 'not_configured',
                'details': {
                    'message': 'No certificate found in etcd',
                    'days_until_expiry': None,
                    'expires_at': None
                }
            }
        
        # Parse the certificate
        cert_pem = cert_value.decode()
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        
        # Get expiry date
        expires_at = cert.not_valid_after
        now = datetime.now(UTC).replace(tzinfo=None)  # Remove timezone for comparison
        
        # Calculate days until expiry
        time_until_expiry = expires_at - now
        days_until_expiry = time_until_expiry.days
        
        # Determine status based on days remaining
        if days_until_expiry < 0:
            status = 'expired'
            message = f'Certificate expired {abs(days_until_expiry)} days ago'
        elif days_until_expiry <= 7:
            status = 'critical'
            message = f'Certificate expires in {days_until_expiry} days'
        elif days_until_expiry <= 30:
            status = 'warning'
            message = f'Certificate expires in {days_until_expiry} days'
        else:
            status = 'healthy'
            message = f'Certificate expires in {days_until_expiry} days'
        
        return {
            'status': status,
            'details': {
                'message': message,
                'days_until_expiry': days_until_expiry,
                'expires_at': expires_at.isoformat(),
                'subject': cert.subject.rfc4514_string(),
                'issuer': cert.issuer.rfc4514_string()
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {
                'message': f'Certificate check failed: {str(e)}',
                'days_until_expiry': None,
                'expires_at': None
            }
        }

def check_clock_skew():
    """Check clock skew using NTP protocol to VIP"""

    # NTP server to check against (VIP)
    ntp_server = '10.0.0.254'

    try:
        # Create NTP client
        client = ntplib.NTPClient()
        
        # Make NTP request (this is a lightweight UDP request)
        response = client.request(ntp_server, version=3, timeout=2)
        
        # Get offset in milliseconds
        offset_ms = response.offset * 1000
        
        # Determine status based on offset
        if abs(offset_ms) > 1000:  # More than 1 second
            status = 'critical'
        elif abs(offset_ms) > 100:  # More than 100ms
            status = 'warning'
        else:
            status = 'healthy'
        
        return {
            'status': status,
            'details': {
                'offset_ms': round(offset_ms, 3),
                'ntp_server': ntp_server,
                'stratum': response.stratum,
                'precision': response.precision,
                'delay': response.delay,
                'message': f'Clock offset: {round(offset_ms, 3)}ms'
            }
        }
        
    except ntplib.NTPException as e:
        return {
            'status': 'error',
            'details': {'message': f'NTP request failed: {str(e)}'}
        }
    except socket.gaierror:
        return {
            'status': 'error',
            'details': {'message': f'Could not resolve NTP server {ntp_server}'}
        }
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Clock skew check failed: {str(e)}'}
        }

def check_docker_daemon():
    """Check Docker daemon status and functionality"""
    try:
        # Check if Docker service is running
        docker_service_running = check_service_status('docker')
        
        # Check if Docker socket is accessible
        docker_socket_accessible = False
        docker_version = None
        docker_error = None
        
        if docker_service_running:
            try:
                # Test Docker daemon connectivity
                result = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'], 
                                      capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    docker_socket_accessible = True
                    docker_version = result.stdout.strip()
                else:
                    docker_error = result.stderr.strip()
            except subprocess.TimeoutExpired:
                docker_error = 'Docker command timeout'
            except Exception as e:
                docker_error = f'Docker command failed: {str(e)}'
        
        # Overall status
        if docker_service_running and docker_socket_accessible:
            status = 'healthy'
            message = f'Docker daemon running (version {docker_version})'
        elif docker_service_running:
            status = 'degraded'
            message = f'Docker service running but daemon not accessible: {docker_error}'
        else:
            status = 'unhealthy'
            message = 'Docker service not running'
        
        return {
            'status': status,
            'details': {
                'service_active': docker_service_running,
                'daemon_accessible': docker_socket_accessible,
                'version': docker_version,
                'message': message,
                'error': docker_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Docker check failed: {str(e)}'}
        }

def check_docker_registry():
    """Check Docker registry status and functionality"""
    try:
        # Check if registry service is running
        registry_service_running = check_service_status('docker-registry')
        
        # Also check if registry container is running directly
        registry_container_running = False

        # Check if registry port is open (try both localhost and storage VIP)
        registry_port_open = check_port_open('localhost', 5000)

        # Test registry health endpoint
        registry_healthy = False
        registry_error = None
        registry_version = None
        
        if registry_port_open:
            # Try both localhost and VIP endpoints
            test_urls = ['http://localhost:5000/v2/', 'http://10.0.0.100:5000/v2/']
            for url in test_urls:
                try:
                    health_response = requests.get(url, timeout=5)
                    if health_response.status_code == 200:
                        registry_healthy = True
                        registry_version = health_response.headers.get('Docker-Distribution-Api-Version', 'unknown')
                        break
                    else:
                        registry_error = f'Registry health check returned HTTP {health_response.status_code}'
                except requests.exceptions.Timeout:
                    registry_error = 'Registry health check timeout'
                except requests.exceptions.ConnectionError:
                    registry_error = 'Registry connection failed'
                except Exception as e:
                    registry_error = f'Registry health check failed: {str(e)}'
        
        # Check if this node should be running the registry (storage leader)
        is_storage_lead = is_storage_leader()
        
        # Registry is considered running if either service or container is running
        registry_running = registry_service_running or registry_container_running
        
        # Determine overall status
        if is_storage_lead:
            if registry_running and registry_port_open and registry_healthy:
                status = 'healthy'
                message = f'Registry running and healthy (API version {registry_version})'
            elif registry_running and registry_port_open:
                status = 'degraded'
                message = f'Registry running but health check failed: {registry_error}'
            elif registry_running:
                status = 'unhealthy'
                message = f'Registry running but port not accessible: {registry_error}'
            else:
                status = 'unhealthy'
                message = 'Registry not running'
        else:
            # Not storage leader - registry should not be running
            if registry_running or registry_port_open:
                status = 'unhealthy'
                message = 'Split-brain: Registry running on non-leader'
            else:
                status = 'not_required'
                message = 'Registry not required (not storage leader)'
        
        return {
            'status': status,
            'details': {
                'service_active': registry_service_running,
                'container_running': registry_container_running,
                'port_open': registry_port_open,
                'health_check_passed': registry_healthy,
                'api_version': registry_version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': registry_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Registry check failed: {str(e)}'}
        }

def check_tang_service():
    """Check Tang server status and functionality"""
    try:
        # Check if Tang service is running
        tang_service_running = check_service_status('tangd.socket')
        
        # Check if Tang port is open
        tang_port_open = check_port_open('localhost', 8777)
        
        # Test Tang advertisement endpoint
        tang_healthy = False
        tang_error = None
        tang_keys = None
        
        if tang_port_open:
            try:
                adv_response = requests.get('http://localhost:8777/adv', timeout=5)
                if adv_response.status_code == 200:
                    tang_healthy = True
                    # Try to parse the advertisement to count keys
                    try:
                        adv_data = adv_response.json()
                        if isinstance(adv_data, dict) and 'keys' in adv_data:
                            tang_keys = len(adv_data['keys'])
                        else:
                            tang_keys = 'unknown'
                    except:
                        tang_keys = 'unknown'
                else:
                    tang_error = f'Tang advertisement returned HTTP {adv_response.status_code}'
            except requests.exceptions.Timeout:
                tang_error = 'Tang advertisement timeout'
            except requests.exceptions.ConnectionError:
                tang_error = 'Tang connection failed'
            except Exception as e:
                tang_error = f'Tang advertisement failed: {str(e)}'
        
        # Determine overall status
        if tang_service_running and tang_port_open and tang_healthy:
            status = 'healthy'
            message = f'Tang server running and healthy ({tang_keys} keys)'
        elif tang_service_running and tang_port_open:
            status = 'degraded'
            message = f'Tang service running but advertisement failed: {tang_error}'
        elif tang_service_running:
            status = 'unhealthy'
            message = f'Tang service running but port not accessible: {tang_error}'
        else:
            status = 'unhealthy'
            message = 'Tang service not running'
        
        return {
            'status': status,
            'details': {
                'service_active': tang_service_running,
                'port_open': tang_port_open,
                'advertisement_working': tang_healthy,
                'key_count': tang_keys,
                'message': message,
                'error': tang_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Tang check failed: {str(e)}'}
        }

def check_secrets_mount():
    """Check if /secrets is mounted"""
    try:
        # Check if /secrets is mounted
        result = subprocess.run(['mountpoint', '-q', '/secrets'], 
                              capture_output=True, text=True, timeout=5)
        is_mounted = result.returncode == 0
        
        # Get mount details if mounted
        mount_details = None
        if is_mounted:
            try:
                mount_result = subprocess.run(['findmnt', '-n', '-o', 'SOURCE,FSTYPE,OPTIONS', '/secrets'], 
                                            capture_output=True, text=True, timeout=5)
                if mount_result.returncode == 0:
                    mount_details = mount_result.stdout.strip()
            except:
                pass
        
        # Check if secrets directory exists and is accessible
        secrets_accessible = False
        secrets_error = None
        try:
            if os.path.exists('/secrets') and os.path.isdir('/secrets'):
                # Try to list the directory to verify access
                os.listdir('/secrets')
                secrets_accessible = True
            else:
                secrets_error = '/secrets directory does not exist'
        except PermissionError:
            secrets_error = 'Permission denied accessing /secrets'
        except Exception as e:
            secrets_error = f'Error accessing /secrets: {str(e)}'
        
        # Determine overall status
        if is_mounted and secrets_accessible:
            status = 'healthy'
            message = f'Secrets volume mounted and accessible'
        elif is_mounted:
            status = 'degraded'
            message = f'Secrets volume mounted but not accessible: {secrets_error}'
        else:
            status = 'unhealthy'
            message = 'Secrets volume not mounted'
        
        return {
            'status': status,
            'details': {
                'mounted': is_mounted,
                'accessible': secrets_accessible,
                'mount_details': mount_details,
                'message': message,
                'error': secrets_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Secrets mount check failed: {str(e)}'}
        }

def check_open_webui():
    """Check Open-WebUI service status and functionality"""
    try:
        # Check if Open-WebUI service is running
        webui_service_running = check_service_status('open-webui')
        
        # Check if Open-WebUI port is open
        webui_port_open = check_port_open('localhost', 8380)
        
        # Test Open-WebUI health endpoint
        webui_healthy = False
        webui_error = None
        webui_version = None
        
        if webui_port_open:
            try:
                # Try health check endpoint
                health_response = requests.get('http://localhost:8380/health', timeout=5)
                if health_response.status_code == 200:
                    webui_healthy = True
                    try:
                        health_data = health_response.json()
                        webui_version = health_data.get('version', 'unknown')
                    except:
                        webui_version = 'unknown'
                else:
                    webui_error = f'Open-WebUI health check returned HTTP {health_response.status_code}'
            except requests.exceptions.Timeout:
                webui_error = 'Open-WebUI health check timeout'
            except requests.exceptions.ConnectionError:
                webui_error = 'Open-WebUI connection failed'
            except Exception as e:
                webui_error = f'Open-WebUI health check failed: {str(e)}'
        
        # Check if this node should be running Open-WebUI (storage leader)
        is_storage_lead = is_storage_leader()
        
        # Determine overall status
        if is_storage_lead:
            if webui_service_running and webui_port_open and webui_healthy:
                status = 'healthy'
                message = f'Open-WebUI running and healthy (version {webui_version})'
            elif webui_service_running and webui_port_open:
                status = 'degraded'
                message = f'Open-WebUI running but health check failed: {webui_error}'
            elif webui_service_running:
                status = 'unhealthy'
                message = f'Open-WebUI service running but port not accessible: {webui_error}'
            else:
                status = 'unhealthy'
                message = 'Open-WebUI service not running'
        else:
            # Not storage leader - Open-WebUI should not be running
            if webui_service_running or webui_port_open:
                status = 'unhealthy'
                message = 'Split-brain: Open-WebUI running on non-leader'
            else:
                status = 'not_required'
                message = 'Open-WebUI not required (not storage leader)'
        
        return {
            'status': status,
            'details': {
                'service_active': webui_service_running,
                'port_open': webui_port_open,
                'health_check_passed': webui_healthy,
                'version': webui_version,
                'required': is_storage_lead,
                'reason': 'storage leader' if is_storage_lead else 'not storage leader',
                'message': message,
                'error': webui_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Open-WebUI check failed: {str(e)}'}
        }

def is_storage_leader():
    """Check if this node is the current storage leader"""
    try:
        client = get_etcd_client()
        result = client.get('/cluster/leader/app')
        if result[0]:
            leader = result[0].decode()
            return leader == platform.node()
        return False
    except:
        return False

def is_dhcp_leader():
    """Check if this node is the current DHCP leader"""
    try:
        client = get_etcd_client()
        result = client.get('/cluster/leader/dhcp')
        if result[0]:
            leader = result[0].decode()
            return leader == platform.node()
        return False
    except:
        return False

def is_node_drained():
    """Check if this node is drained"""
    try:
        hostname = platform.node()
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{hostname}/drain')
        return result[0] is not None and result[0].decode() == 'true'
    except:
        return False

@app.route('/api/drain', methods=['POST'])
def drain_node():
    """Drain this node - disable leader election"""
    try:
        hostname = platform.node()
        client = get_etcd_client()
        client.put(f'/cluster/nodes/{hostname}/drain', 'true')
        return jsonify({'status': 'drained', 'hostname': hostname})
    except Exception as e:
        return jsonify({'error': f'Failed to drain node: {str(e)}'}), 500

@app.route('/api/undrain', methods=['POST']) 
def undrain_node():
    """Undrain this node - re-enable leader election"""
    try:
        hostname = platform.node()
        client = get_etcd_client()
        client.delete(f'/cluster/nodes/{hostname}/drain')
        return jsonify({'status': 'active', 'hostname': hostname})
    except Exception as e:
        return jsonify({'error': f'Failed to undrain node: {str(e)}'}), 500

@app.route('/api/drain/<target_hostname>', methods=['POST'])
def drain_target_node(target_hostname):
    """Drain a specific node - disable leader election"""
    try:
        client = get_etcd_client()
        client.put(f'/cluster/nodes/{target_hostname}/drain', 'true')
        return jsonify({'status': 'drained', 'hostname': target_hostname})
    except Exception as e:
        return jsonify({'error': f'Failed to drain node {target_hostname}: {str(e)}'}), 500

@app.route('/api/undrain/<target_hostname>', methods=['POST'])
def undrain_target_node(target_hostname):
    """Undrain a specific node - re-enable leader election"""
    try:
        client = get_etcd_client()
        client.delete(f'/cluster/nodes/{target_hostname}/drain')
        return jsonify({'status': 'active', 'hostname': target_hostname})
    except Exception as e:
        return jsonify({'error': f'Failed to undrain node {target_hostname}: {str(e)}'}), 500

@app.route('/api/drain/status')
def drain_status():
    """Check drain status of this node"""
    try:
        hostname = platform.node()
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{hostname}/drain')
        is_drained = result[0] is not None and result[0].decode() == 'true'
        return jsonify({'hostname': hostname, 'drained': is_drained})
    except Exception as e:
        return jsonify({'error': f'Failed to check drain status: {str(e)}'}), 500

@app.route('/api/drain/status/<target_hostname>')
def drain_status_target(target_hostname):
    """Check drain status of a specific node"""
    try:
        client = get_etcd_client()
        result = client.get(f'/cluster/nodes/{target_hostname}/drain')
        is_drained = result[0] is not None and result[0].decode() == 'true'
        return jsonify({'hostname': target_hostname, 'drained': is_drained})
    except Exception as e:
        return jsonify({'error': f'Failed to check drain status for {target_hostname}: {str(e)}'}), 500

@app.route('/api/ping')
def ping():
    """Simple ping endpoint for connectivity testing"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now(UTC).isoformat()})

@app.route('/api/time')
def get_time():
    """Get current timestamp for clock synchronization checks"""
    return jsonify({'timestamp': time.time()})

def get_mac_from_ip(client_ip):
    """
    Look up MAC address from IP address using DHCP leases in etcd or neighbor table.
    Return value is non-normalized MAC address (xx:xx:xx:xx:xx:xx) or None if not found.
    """
    if not client_ip:
        return None
    
    client = get_etcd_client()

    # Look through DHCP leases in etcd
    for value, metadata in client.get_prefix('/cluster/dhcp/leases/'):
        if value:
            try:
                lease_data = json.loads(value.decode())
                if lease_data.get('ip') == client_ip:
                    # Return non-normalized MAC (with colons) from lease data
                    return lease_data.get('mac')
            except json.JSONDecodeError:
                # Skip non-JSON entries in the dhcp prefix
                continue

    print("fallback to neighbor table", client_ip, file=sys.stderr)

    # Fallback to neighbor table lookup using ip --json neigh
    result = None
    try:
        result = subprocess.run(['ip', '--json', 'neigh', 'show', client_ip], 
                              capture_output=True, text=True, timeout=5)
        neighbors = json.loads(result.stdout)
        for i, neighbor in enumerate(neighbors):
            print(f"neighbor {i}: {neighbor}", file=sys.stderr)
            if neighbor.get('dst') == client_ip and 'lladdr' in neighbor:
                mac = neighbor['lladdr']
                # Validate MAC format (should be xx:xx:xx:xx:xx:xx)
                if isinstance(mac, str) and len(mac) == 17 and mac.count(':') == 5:
                    return mac
                else:
                    print(f"MAC validation failed", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"neighbor table JSON parse error: {e}", file=sys.stderr)
        print(f"Raw stdout was: {result.stdout}", file=sys.stderr)
    except Exception as e:
        print(f"neighbor table lookup failed for {client_ip}: {e}", file=sys.stderr)

    return None

@app.route('/autoinstall/meta-data')
def serve_meta_data():
    """Serve empty meta-data for autoinstall"""
    return "", 200, {'Content-Type': 'text/plain'}

@app.route('/autoinstall/user-data')
def serve_user_data():
    """Serve dynamically rendered user-data based on client MAC address"""
    # Get client IP address
    client_ip = request.environ.get('REMOTE_ADDR') or request.remote_addr
    
    # Look up MAC address from IP
    mac_address = get_mac_from_ip(client_ip)
    if not mac_address:
        return f"MAC address not found for client IP {client_ip}", 400

    print(f"Client IP: {client_ip}, MAC: {mac_address}", file=sys.stderr)
    
    # Get or create allocation for this MAC address
    allocation_data = get_or_create_allocation(mac_address)
    
    # Use allocation data
    node_type = allocation_data['type']
    hostname = allocation_data['hostname']
    ip_address = allocation_data['ip']
    amt_ip_address = allocation_data['amt_ip']
    
    # Get interface configuration for this node type
    interfaces = NODE_TYPE_INTERFACES[node_type]
    
    # Get SSH public key content
    ssh_key_path = '/opt/bootstrap-files/ansible_ssh_key.pub'
    with open(ssh_key_path, 'r') as f:
        ssh_key_content = f.read().strip()

    # Get crypted password for ubuntu user
    ubuntu_password = None
    try:
        with open('/etc/shadow', 'r') as f:
            for line in f:
                fields = line.strip().split(':')
                if fields[0] == 'ubuntu' and len(fields) > 1:
                    ubuntu_password = fields[1]
                    break
    except (PermissionError, FileNotFoundError):
        raise Exception("Cannot read ubuntu password from /etc/shadow - check permissions")
    
    if not ubuntu_password:
        raise Exception("Ubuntu user not found in /etc/shadow")

    proxy_url = 'http://10.0.0.254:3128'
    
    # Read and render template
    with open(AUTOINSTALL_USER_DATA_TEMPLATE, 'r') as f:
        template_content = f.read()
    
    template = Template(template_content)
    rendered_content = template.render(
        node_type=node_type,
        hostname=hostname,
        ip_address=ip_address,
        amt_ip_address=amt_ip_address,
        cluster_interface=interfaces['cluster_interface'],
        uplink_interface=interfaces['uplink_interface'],
        amt_interface=interfaces['amt_interface'],
        ssh_key_content=ssh_key_content,
        ubuntu_password=ubuntu_password,
        proxy_url=proxy_url
    )
    
    return rendered_content, 200, {'Content-Type': 'text/plain'}

@app.route('/api/health')
def health():
    """Comprehensive health check endpoint for all services"""
    health_status = {
        'overall': 'healthy',
        'services': {}
    }
    
    # Check etcd
    try:
        client = get_etcd_client()
        client.get('/test')
        health_status['services']['etcd'] = {'status': 'healthy', 'details': 'connected'}
    except Exception as e:
        health_status['services']['etcd'] = {'status': 'unhealthy', 'details': str(e)}
        health_status['overall'] = 'unhealthy'
    
    # Check Ceph storage
    ceph_health = check_ceph_status()
    health_status['services']['ceph'] = ceph_health
    if ceph_health['status'] not in ['healthy', 'degraded']:
        health_status['overall'] = 'unhealthy'
    
    # Check PostgreSQL (always check, flag split-brain if running on non-leader)
    postgres_running = check_service_status('postgresql@16-main')
    postgres_port = check_port_open('localhost', 5432)
    is_storage_lead = is_storage_leader()
    
    if is_storage_lead:
        postgres_healthy = postgres_running and postgres_port
        health_status['services']['postgresql'] = {
            'status': 'healthy' if postgres_healthy else 'unhealthy',
            'details': {
                'service_active': postgres_running,
                'port_open': postgres_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not postgres_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if postgres_running or postgres_port:
            health_status['services']['postgresql'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': postgres_running,
                    'port_open': postgres_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['postgresql'] = {
                'status': 'not_required',
                'details': {
                    'service_active': postgres_running,
                    'port_open': postgres_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check Qdrant (always check, flag split-brain if running on non-leader)
    qdrant_running = check_service_status('qdrant')
    qdrant_port = check_port_open('localhost', 6333)
    
    if is_storage_lead:
        qdrant_healthy = qdrant_running and qdrant_port
        health_status['services']['qdrant'] = {
            'status': 'healthy' if qdrant_healthy else 'unhealthy',
            'details': {
                'service_active': qdrant_running,
                'port_open': qdrant_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not qdrant_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if qdrant_running or qdrant_port:
            health_status['services']['qdrant'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': qdrant_running,
                    'port_open': qdrant_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['qdrant'] = {
                'status': 'not_required',
                'details': {
                    'service_active': qdrant_running,
                    'port_open': qdrant_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check storage leader election
    storage_leader_running = check_service_status('storage-leader-election')
    health_status['services']['storage_leader_election'] = {
        'status': 'healthy' if storage_leader_running else 'unhealthy',
        'details': {'service_active': storage_leader_running}
    }
    if not storage_leader_running:
        health_status['overall'] = 'unhealthy'
    
    # Check DHCP leader election
    dhcp_leader_running = check_service_status('dhcp-leader-election')
    health_status['services']['dhcp_leader_election'] = {
        'status': 'healthy' if dhcp_leader_running else 'unhealthy',
        'details': {'service_active': dhcp_leader_running}
    }
    if not dhcp_leader_running:
        health_status['overall'] = 'unhealthy'
    
    # Check DHCP (only required if we are DHCP leader)
    is_dhcp_lead = is_dhcp_leader()
    dhcp_port = check_port_open('localhost', 8067)  # DHCP health port
    
    if is_dhcp_lead:
        health_status['services']['dhcp'] = {
            'status': 'healthy' if dhcp_port else 'unhealthy',
            'details': {
                'health_port_open': dhcp_port,
                'required': True,
                'reason': 'dhcp leader'
            }
        }
        if not dhcp_port:
            health_status['overall'] = 'unhealthy'
    else:
        health_status['services']['dhcp'] = {
            'status': 'not_required',
            'details': {
                'health_port_open': dhcp_port,
                'required': False,
                'reason': 'not dhcp leader'
            }
        }
    
    # Check DNS (dnsmasq)
    dns_health = check_dns_status()
    health_status['services']['dns'] = dns_health
    if dns_health['status'] == 'unhealthy':
        health_status['overall'] = 'unhealthy'
    
    # Check Squid proxy
    squid_running = check_service_status('squid')
    squid_port = check_port_open('localhost', 3128)
    squid_functional = False
    squid_error = None
    
    if squid_running and squid_port:
        # Test actual proxy functionality using local ping endpoint
        try:
            # Test a simple HTTP request through the proxy to our own ping endpoint
            proxy_response = requests.get(
                'http://localhost:12723/api/ping',
                proxies={'http': 'http://localhost:3128'},
                timeout=5
            )
            if proxy_response.status_code in [200, 503]:
                squid_functional = True
            else:
                squid_error = f'HTTP {proxy_response.status_code}'
        except requests.exceptions.ProxyError as e:
            squid_error = f'Proxy error: {str(e)}'
        except requests.exceptions.Timeout:
            squid_error = 'Proxy timeout'
        except Exception as e:
            squid_error = f'Proxy test failed: {str(e)}'
    
    squid_healthy = squid_running and squid_port and squid_functional
    health_status['services']['squid'] = {
        'status': 'healthy' if squid_healthy else 'unhealthy',
        'details': {
            'service_active': squid_running,
            'port_open': squid_port,
            'proxy_functional': squid_functional,
            'error': squid_error
        }
    }
    if not squid_healthy:
        health_status['overall'] = 'unhealthy'
    
    # Check NTP
    ntp_running = check_service_status('ntp') or check_service_status('chrony')
    health_status['services']['ntp'] = {
        'status': 'healthy' if ntp_running else 'unhealthy',
        'details': {'service_active': ntp_running}
    }
    if not ntp_running:
        health_status['overall'] = 'unhealthy'
    
    # Check TLS certificate expiry
    cert_health = check_certificate_expiry()
    health_status['services']['tls_certificate'] = cert_health
    if cert_health['status'] in ['expired', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif cert_health['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check rathole (only required if we are storage leader)
    rathole_running = check_service_status('rathole')
    rathole_port = check_port_open('localhost', 2333)  # Default rathole client port
    
    if is_storage_lead:
        rathole_healthy = rathole_running
        health_status['services']['rathole'] = {
            'status': 'healthy' if rathole_healthy else 'unhealthy',
            'details': {
                'service_active': rathole_running,
                'port_open': rathole_port,
                'required': True,
                'reason': 'storage leader'
            }
        }
        if not rathole_healthy:
            health_status['overall'] = 'unhealthy'
    else:
        # Not leader but check for split-brain
        if rathole_running:
            health_status['services']['rathole'] = {
                'status': 'unhealthy',
                'details': {
                    'service_active': rathole_running,
                    'port_open': rathole_port,
                    'required': False,
                    'reason': 'split-brain: running on non-leader'
                }
            }
            health_status['overall'] = 'unhealthy'
        else:
            health_status['services']['rathole'] = {
                'status': 'not_required',
                'details': {
                    'service_active': rathole_running,
                    'port_open': rathole_port,
                    'required': False,
                    'reason': 'not storage leader'
                }
            }
    
    # Check clock skew
    clock_skew = check_clock_skew()
    health_status['services']['clock_skew'] = clock_skew
    if clock_skew['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif clock_skew['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check Docker daemon
    docker_daemon = check_docker_daemon()
    health_status['services']['docker_daemon'] = docker_daemon
    if docker_daemon['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif docker_daemon['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check Docker registry
    docker_registry = check_docker_registry()
    health_status['services']['docker_registry'] = docker_registry
    if docker_registry['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif docker_registry['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check Tang service
    tang_service = check_tang_service()
    health_status['services']['tang'] = tang_service
    if tang_service['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif tang_service['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check secrets mount
    secrets_mount = check_secrets_mount()
    health_status['services']['secrets_mount'] = secrets_mount
    if secrets_mount['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif secrets_mount['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check Open-WebUI
    open_webui = check_open_webui()
    health_status['services']['open_webui'] = open_webui
    if open_webui['status'] in ['unhealthy', 'error']:
        health_status['overall'] = 'unhealthy'
    elif open_webui['status'] == 'degraded' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check VIP status
    vip_health = check_vip_status()
    gateway_vip_active = vip_health['gateway_vip']['active']
    storage_vip_active = vip_health['storage_vip']['active']
    
    health_status['services']['gateway_vip'] = {
        'status': 'healthy' if gateway_vip_active else 'not_required',
        'details': vip_health['gateway_vip']
    }
    
    health_status['services']['storage_vip'] = {
        'status': 'healthy' if storage_vip_active else 'not_required',
        'details': vip_health['storage_vip']
    }
    
    # Check keepalived service (only on core nodes)
    current_hostname = platform.node()
    if current_hostname in CORE_NODES:
        keepalived_running = check_service_status('keepalived')
        health_status['services']['keepalived'] = {
            'status': 'healthy' if keepalived_running else 'unhealthy',
            'details': {'service_active': keepalived_running}
        }
        if not keepalived_running:
            health_status['overall'] = 'unhealthy'
    else:
        # Not a core node - keepalived should not be running
        keepalived_running = check_service_status('keepalived')
        health_status['services']['keepalived'] = {
            'status': 'not_required',
            'details': {
                'service_active': keepalived_running,
                'reason': 'not a core node'
            }
        }
    
    # Add leadership status for this node
    health_status['storage_leader'] = is_storage_leader()
    health_status['dhcp_leader'] = is_dhcp_leader()
    health_status['drained'] = is_node_drained()
    
    # Return appropriate HTTP status code
    status_code = 200 if health_status['overall'] == 'healthy' else 503
    return jsonify(health_status), status_code

def get_all_hosts():
    """Get all hosts from etcd allocations"""
    try:
        client = get_etcd_client()
        hosts = []
        
        # Get all allocations from by-hostname
        for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
            if value:
                try:
                    allocation = json.loads(value.decode())
                    hostname = allocation['hostname']
                    
                    # Skip AMT interfaces (hostnames ending with 'a')
                    if hostname.endswith('a'):
                        continue
                    
                    # Skip dynamic IP allocations (hostnames that don't match expected patterns)
                    # Expected patterns: s1-s20, c1-c20, m1-m20
                    if not (hostname.startswith(('s', 'c', 'm')) and 
                            len(hostname) > 1 and 
                            hostname[1:].isdigit()):
                        continue
                        
                    hosts.append({
                        'hostname': hostname,
                        'ip': allocation['ip'],
                        'type': allocation['type']
                    })
                except:
                    pass
        
        # Sort by hostname
        hosts.sort(key=lambda x: (x['type'], int(x['hostname'][1:]) if x['hostname'][1:].isdigit() else 0))
        return hosts
    except:
        return []

def get_host_health(host_ip, timeout=5):
    """Get health status from a specific host"""
    try:
        response = requests.get(f"http://{host_ip}:12723/api/health", timeout=timeout)
        if response.status_code in [200, 503]:
            # Both 200 (healthy) and 503 (unhealthy) contain valid health data
            return response.json()
        else:
            return {'overall': 'error', 'services': {}, 'error': f'HTTP {response.status_code}'}
    except requests.exceptions.Timeout:
        return {'overall': 'timeout', 'services': {}, 'error': 'Request timeout'}
    except requests.exceptions.ConnectionError:
        return {'overall': 'unreachable', 'services': {}, 'error': 'Connection failed'}
    except Exception as e:
        return {'overall': 'error', 'services': {}, 'error': str(e)}

def check_vip_status():
    """Check VIP status using keepalived and ip commands"""
    gateway_vip_ip = '10.0.0.254'
    storage_vip_ip = '10.0.0.100'
    vip_status = {
        'gateway_vip': {
            'ip': gateway_vip_ip,
            'active': False,
            'master': None,
            'interface': None
        },
        'storage_vip': {
            'ip': storage_vip_ip,
            'active': False,
            'master': None,
            'interface': None
        }
    }
    
    # Check gateway VIP
    try:
        # Use 'ip -j addr show to <vip>' to get JSON output for reliable parsing
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', gateway_vip_ip], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            # Parse JSON output - if there's any output, VIP is active on this node
            interfaces = json.loads(result.stdout)
            if interfaces:
                # VIP is assigned to this node
                vip_status['gateway_vip']['active'] = True
                vip_status['gateway_vip']['master'] = platform.node()
                # Get interface name from first interface in results
                vip_status['gateway_vip']['interface'] = interfaces[0].get('ifname')
        else:
            # No output means VIP is not assigned to this node
            vip_status['gateway_vip']['active'] = False
            
    except json.JSONDecodeError as e:
        vip_status['gateway_vip']['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        vip_status['gateway_vip']['error'] = str(e)
    
    # Check storage VIP
    try:
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', storage_vip_ip], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            interfaces = json.loads(result.stdout)
            if interfaces:
                vip_status['storage_vip']['active'] = True
                vip_status['storage_vip']['master'] = platform.node()
                vip_status['storage_vip']['interface'] = interfaces[0].get('ifname')
        else:
            vip_status['storage_vip']['active'] = False
            
    except json.JSONDecodeError as e:
        vip_status['storage_vip']['error'] = f'JSON parse error: {str(e)}'
    except Exception as e:
        vip_status['storage_vip']['error'] = str(e)
    
    # Check keepalived service status
    try:
        keepalived_running = check_service_status('keepalived')
        vip_status['keepalived_service'] = {
            'active': keepalived_running,
            'status': 'running' if keepalived_running else 'stopped'
        }
    except Exception as e:
        vip_status['keepalived_service'] = {
            'active': False,
            'status': 'error',
            'error': str(e)
        }
    
    return vip_status

def get_cluster_vip_status(host_health):
    """Get VIP status across all cluster nodes from existing health data"""
    vip_info = {
        'gateway_vip': {
            'ip': '10.0.0.254',
            'active_on': None,
            'master_hostname': None,
            'interface': None
        },
        'storage_vip': {
            'ip': '10.0.0.100',
            'active_on': None,
            'master_hostname': None,
            'interface': None
        },
        'keepalived_nodes': []  # Single list for keepalived status across all nodes
    }
    
    # Get all hosts to find core nodes
    all_hosts = get_all_hosts()
    
    # Process only core nodes (where keepalived runs and VIPs can be active)
    for core_node in CORE_NODES:
        # Find the core node in all_hosts to get its IP
        core_host = next((host for host in all_hosts if host['hostname'] == core_node), None)
        if not core_host:
            # Core node not found in allocations - add as missing
            vip_info['keepalived_nodes'].append({
                'hostname': core_node,
                'ip': 'unknown',
                'keepalived_active': False,
                'status': 'not_allocated'
            })
            continue
        
        hostname = core_host['hostname']
        host_ip = core_host['ip']
        health_data = host_health.get(core_node, {})
        
        if 'error' in health_data or 'services' not in health_data:
            vip_info['keepalived_nodes'].append({
                'hostname': core_node,
                'ip': host_ip,
                'keepalived_active': False,
                'status': 'unreachable'
            })
            continue
        
        # Process VIP status
        gateway_vip_service = health_data.get('services', {}).get('gateway_vip', {})
        storage_vip_service = health_data.get('services', {}).get('storage_vip', {})
        
        # Process gateway VIP
        gateway_vip_details = gateway_vip_service.get('details', {})
        if gateway_vip_details and gateway_vip_details.get('active', False):
            vip_info['gateway_vip']['active_on'] = host_ip
            vip_info['gateway_vip']['master_hostname'] = hostname
            vip_info['gateway_vip']['interface'] = gateway_vip_details.get('interface')
        
        # Process storage VIP
        storage_vip_details = storage_vip_service.get('details', {})
        if storage_vip_details and storage_vip_details.get('active', False):
            vip_info['storage_vip']['active_on'] = host_ip
            vip_info['storage_vip']['master_hostname'] = hostname
            vip_info['storage_vip']['interface'] = storage_vip_details.get('interface')
        
        # Process keepalived service status
        keepalived_service = health_data.get('services', {}).get('keepalived', {})
        if keepalived_service:
            keepalived_active = keepalived_service.get('details', {}).get('service_active', False)
            keepalived_status = 'running' if keepalived_active else 'stopped'
        else:
            keepalived_active = False
            keepalived_status = 'no_data'
        
        vip_info['keepalived_nodes'].append({
            'hostname': core_node,
            'ip': host_ip,
            'keepalived_active': keepalived_active,
            'status': keepalived_status
        })
    
    return vip_info


def get_leadership_status():
    """Get current leadership status from etcd"""
    try:
        client = get_etcd_client()
        leadership = {}
        
        # Get storage leader
        result = client.get('/cluster/leader/app')
        if result[0]:
            storage_leader = result[0].decode()
            leadership['storage_leader'] = storage_leader
        
        # Get DHCP leader
        result = client.get('/cluster/leader/dhcp')
        if result[0]:
            dhcp_leader = result[0].decode()
            leadership['dhcp_leader'] = dhcp_leader
            
        return leadership
    except:
        return {}

@app.route('/api/cluster-status')
def cluster_status_api():
    """API endpoint returning cluster status as JSON"""
    hosts = get_all_hosts()
    host_health = {}
    leadership = get_leadership_status()
    certificate_status = check_certificate_expiry()

    # Get health status for each host
    for host in hosts:
        host_health[host['hostname']] = get_host_health(host['ip'])
    
    # Extract VIP status from existing health data
    vip_status = get_cluster_vip_status(host_health)
    
    return jsonify({
        'hosts': hosts,
        'hostHealth': host_health,
        'leadership': leadership,
        'vipStatus': vip_status,
        'certificateStatus': certificate_status,
        'respondingHostname': platform.node(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files"""
    import os
    from flask import Response
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(script_dir, 'static')
    
    # Get the response first
    response = send_from_directory(static_dir, filename)
    
    # Set correct MIME type for JavaScript files
    if filename.endswith('.js'):
        response.headers['Content-Type'] = 'application/javascript'
    
    return response

@app.route('/status')
def status_page():
    """Web page showing cluster-wide health status"""
    hosts = get_all_hosts()
    host_health = {}
    leadership = get_leadership_status()
    certificate_status = check_certificate_expiry()

    # Get health status for each host
    for host in hosts:
        host_health[host['hostname']] = get_host_health(host['ip'])
    
    # Extract VIP status from existing health data
    vip_status = get_cluster_vip_status(host_health)
    
    # Get drain status for this node
    current_node_drained = is_node_drained()
    
    return render_template('status.html', 
                         hosts=hosts, 
                         host_health=host_health,
                         leadership=leadership,
                         vip_status=vip_status,
                         certificate_status=certificate_status,
                         responding_hostname=platform.node(),
                         current_node_drained=current_node_drained,
                         timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

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
