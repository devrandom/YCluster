import json
import etcd3
from flask import Flask, request, jsonify, render_template
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
        amt_ip_address = determine_ip_from_hostname(hostname + "a")

        # Create allocation record
        allocation = {
            'hostname': hostname,
            'type': machine_type,
            'ip': ip_address,
            'amt_ip': amt_ip_address,
            'mac': normalized_mac,
            'allocated_at': datetime.now(UTC).isoformat()
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

@app.route('/api/ping')
def ping():
    """Simple ping endpoint for connectivity testing"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now(UTC).isoformat()})

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
    vip_ip = '10.0.0.254'
    vip_status = {
        'gateway_vip': {
            'ip': vip_ip,
            'active': False,
            'master': None,
            'interface': None
        }
    }
    
    try:
        # Use 'ip -j addr show to <vip>' to get JSON output for reliable parsing
        result = subprocess.run(['ip', '-j', 'addr', 'show', 'to', vip_ip], 
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

@app.route('/status')
def status_page():
    """Web page showing cluster-wide health status"""
    hosts = get_all_hosts()
    host_health = {}
    leadership = get_leadership_status()
    vip_status = check_vip_status()
    certificate_status = check_certificate_expiry()
    
    # Get health status for each host
    for host in hosts:
        host_health[host['hostname']] = get_host_health(host['ip'])
    
    return render_template('status.html', 
                         hosts=hosts, 
                         host_health=host_health,
                         leadership=leadership,
                         vip_status=vip_status,
                         certificate_status=certificate_status,
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
