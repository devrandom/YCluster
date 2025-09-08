#!/usr/bin/env python3
"""
macOS Health Service

A lightweight health reporting service for macOS nodes that's compatible
with the YCluster health monitoring system.
"""

import subprocess
import socket
import platform
import time
import ntplib
from datetime import datetime, timezone
from flask import Flask, jsonify
import os

app = Flask(__name__)

def check_service_status(service_name):
    """Check if a launchd service is running on macOS"""
    try:
        # Use launchctl to check service status
        result = subprocess.run(['launchctl', 'list', service_name], 
                              capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and service_name in result.stdout
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

def check_ntp_status():
    """Check NTP configuration and synchronization on macOS"""
    try:
        # Check NTP server configuration
        ntp_server_result = subprocess.run(['systemsetup', '-getnetworktimeserver'], 
                                         capture_output=True, text=True, timeout=10)
        
        # Check if automatic time setting is enabled
        auto_time_result = subprocess.run(['systemsetup', '-getusingnetworktime'], 
                                        capture_output=True, text=True, timeout=10)
        
        ntp_server_configured = ntp_server_result.returncode == 0
        auto_time_enabled = auto_time_result.returncode == 0 and 'On' in auto_time_result.stdout
        
        # Try to get NTP server from output
        ntp_server = 'unknown'
        if ntp_server_configured and ntp_server_result.stdout:
            # Parse "Network Time Server: 10.0.0.254" format
            lines = ntp_server_result.stdout.strip().split('\n')
            for line in lines:
                if 'Network Time Server:' in line:
                    ntp_server = line.split(':', 1)[1].strip()
                    break
        
        # Test NTP sync with configured server
        ntp_sync_working = False
        ntp_sync_error = None
        
        if ntp_server and ntp_server != 'unknown':
            try:
                sync_result = subprocess.run(['sntp', ntp_server],
                                           capture_output=True, text=True, timeout=10)
                ntp_sync_working = sync_result.returncode == 0
                if not ntp_sync_working:
                    ntp_sync_error = sync_result.stderr.strip()
            except Exception as e:
                ntp_sync_error = f'NTP sync test failed: {str(e)}'
        
        # Overall status
        if ntp_server_configured and auto_time_enabled and ntp_sync_working:
            status = 'healthy'
            message = f'NTP configured and syncing with {ntp_server}'
        elif ntp_server_configured and auto_time_enabled:
            status = 'degraded'
            message = f'NTP configured but sync test failed: {ntp_sync_error}'
        else:
            status = 'unhealthy'
            message = 'NTP not properly configured'
        
        return {
            'status': status,
            'details': {
                'ntp_server': ntp_server,
                'auto_time_enabled': auto_time_enabled,
                'sync_working': ntp_sync_working,
                'message': message,
                'error': ntp_sync_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'NTP check failed: {str(e)}'}
        }

def check_dns_status():
    """Check DNS configuration on macOS"""
    try:
        # Get DNS servers for Ethernet interface (if available)
        ethernet_dns_result = subprocess.run(['networksetup', '-getdnsservers', 'Ethernet'], 
                                           capture_output=True, text=True, timeout=10)
        
        ethernet_dns_configured = ethernet_dns_result.returncode == 0
        
        # Parse DNS servers
        ethernet_dns_servers = []
        
        if ethernet_dns_configured and ethernet_dns_result.stdout:
            ethernet_dns_servers = [line.strip() for line in ethernet_dns_result.stdout.strip().split('\n') 
                                  if line.strip() and not line.startswith('There aren')]
        
        # Test DNS resolution
        dns_working = False
        dns_error = None
        
        try:
            # Test DNS resolution using nslookup
            dns_test_result = subprocess.run(['nslookup', 's1.xc'],
                                           capture_output=True, text=True, timeout=5)
            dns_working = dns_test_result.returncode == 0 and 's1.xc' in dns_test_result.stdout
            if not dns_working:
                dns_error = 'DNS resolution test failed'
        except Exception as e:
            dns_error = f'DNS test failed: {str(e)}'
        
        # Check if cluster DNS server (10.0.0.254) is configured
        cluster_dns_configured = '10.0.0.254' in ethernet_dns_servers
        
        # Overall status
        if cluster_dns_configured and dns_working:
            status = 'healthy'
            message = 'DNS configured and working'
        elif cluster_dns_configured:
            status = 'degraded'
            message = f'DNS configured but resolution test failed: {dns_error}'
        else:
            status = 'unhealthy'
            message = 'Cluster DNS server not configured'
        
        return {
            'status': status,
            'details': {
                'ethernet_dns_servers': ethernet_dns_servers,
                'cluster_dns_configured': cluster_dns_configured,
                'dns_working': dns_working,
                'message': message,
                'error': dns_error
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'DNS check failed: {str(e)}'}
        }

def check_network_connectivity():
    """Check network connectivity to cluster services"""
    cluster_services = {
        'gateway': ('10.0.0.254', 80),
        'storage': ('10.0.0.100', 5000),
        'dns': ('10.0.0.254', 53)
    }
    
    connectivity_status = {}
    overall_healthy = True
    
    for service_name, (host, port) in cluster_services.items():
        is_reachable = check_port_open(host, port, timeout=3)
        connectivity_status[service_name] = {
            'host': host,
            'port': port,
            'reachable': is_reachable
        }
        if not is_reachable:
            overall_healthy = False
    
    return {
        'status': 'healthy' if overall_healthy else 'degraded',
        'details': {
            'services': connectivity_status,
            'message': 'All cluster services reachable' if overall_healthy else 'Some cluster services unreachable'
        }
    }

def check_disk_space():
    """Check disk space on macOS"""
    try:
        # Use df to get disk usage for root filesystem
        result = subprocess.run(['df', '-h', '/'], 
                              capture_output=True, text=True, timeout=5)
        
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                # Parse df output: Filesystem Size Used Avail Capacity Mounted
                fields = lines[1].split()
                if len(fields) >= 5:
                    size = fields[1]
                    used = fields[2]
                    available = fields[3]
                    capacity_str = fields[4]
                    
                    # Extract percentage from capacity (e.g., "85%" -> 85)
                    capacity_pct = int(capacity_str.rstrip('%'))
                    
                    # Determine status based on usage
                    if capacity_pct >= 95:
                        status = 'critical'
                        message = f'Disk space critical: {capacity_pct}% used'
                    elif capacity_pct >= 85:
                        status = 'warning'
                        message = f'Disk space low: {capacity_pct}% used'
                    else:
                        status = 'healthy'
                        message = f'Disk space OK: {capacity_pct}% used'
                    
                    return {
                        'status': status,
                        'details': {
                            'size': size,
                            'used': used,
                            'available': available,
                            'capacity_percent': capacity_pct,
                            'message': message
                        }
                    }
        
        return {
            'status': 'error',
            'details': {'message': 'Could not parse disk usage'}
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'Disk space check failed: {str(e)}'}
        }

def check_system_load():
    """Check system load on macOS"""
    try:
        # Get load averages
        load_avg = os.getloadavg()
        load_1min, load_5min, load_15min = load_avg
        
        # Get CPU count for context
        cpu_count = os.cpu_count() or 1
        
        # Calculate load percentage (load average / CPU count)
        load_pct = (load_1min / cpu_count) * 100
        
        # Determine status based on load
        if load_pct >= 90:
            status = 'critical'
            message = f'System load critical: {load_pct:.1f}%'
        elif load_pct >= 70:
            status = 'warning'
            message = f'System load high: {load_pct:.1f}%'
        else:
            status = 'healthy'
            message = f'System load OK: {load_pct:.1f}%'
        
        return {
            'status': status,
            'details': {
                'load_1min': load_1min,
                'load_5min': load_5min,
                'load_15min': load_15min,
                'cpu_count': cpu_count,
                'load_percent': round(load_pct, 1),
                'message': message
            }
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'System load check failed: {str(e)}'}
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

@app.route('/api/health')
def health():
    """Comprehensive health check endpoint for macOS nodes"""
    health_status = {
        'overall': 'healthy',
        'services': {},
        'node_type': 'macos',
        'hostname': platform.node(),
        'platform': platform.platform()
    }
    
    # Check NTP
    ntp_health = check_ntp_status()
    health_status['services']['ntp'] = ntp_health
    if ntp_health['status'] in ['unhealthy', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif ntp_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check DNS
    dns_health = check_dns_status()
    health_status['services']['dns'] = dns_health
    if dns_health['status'] in ['unhealthy', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif dns_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check network connectivity
    network_health = check_network_connectivity()
    health_status['services']['network_connectivity'] = network_health
    if network_health['status'] in ['unhealthy', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif network_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check disk space
    disk_health = check_disk_space()
    health_status['services']['disk_space'] = disk_health
    if disk_health['status'] in ['critical']:
        health_status['overall'] = 'unhealthy'
    elif disk_health['status'] in ['warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check system load
    load_health = check_system_load()
    health_status['services']['system_load'] = load_health
    if load_health['status'] in ['critical']:
        health_status['overall'] = 'unhealthy'
    elif load_health['status'] in ['warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Check clock skew
    clock_skew = check_clock_skew()
    health_status['services']['clock_skew'] = clock_skew
    if clock_skew['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif clock_skew['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'
    
    # Return appropriate HTTP status code
    status_code = 200 if health_status['overall'] == 'healthy' else 503
    return jsonify(health_status), status_code

@app.route('/api/ping')
def ping():
    """Simple ping endpoint for connectivity testing"""
    return jsonify({
        'status': 'ok', 
        'hostname': platform.node(),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

@app.route('/api/time')
def get_time():
    """Get current timestamp for clock synchronization checks"""
    return jsonify({'timestamp': time.time()})

if __name__ == '__main__':
    print(f"Starting macOS health service on {platform.node()}")
    app.run(host='0.0.0.0', port=12723)
