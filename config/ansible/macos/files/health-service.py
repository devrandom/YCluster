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
import logging
import sys
from datetime import datetime, timezone
from flask import Flask, jsonify
import os

app = Flask(__name__)

# Per-request + per-check timing. Logs go to stderr, which launchd pipes
# to /var/log/ycluster-health.log (StandardErrorPath in the plist).
# Without these the macOS service has zero visibility into *which* check
# is slow when /api/health crosses the timeout budget — the storage
# leader's cluster-status fan-out only sees the total elapsed time.
log = logging.getLogger('ycluster.health')
log.setLevel(logging.INFO)
log.propagate = False  # waitress installs a root handler with its own formatter;
                       # without this, every record is written twice.
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter('%(asctime)s health %(message)s'))
    log.addHandler(_h)

def check_service_status(service_name):
    """Check if a launchd service is running on macOS"""
    try:
        # Use launchctl to check service status
        result = subprocess.run(['launchctl', 'list', service_name], 
                              capture_output=True, text=True, timeout=3)
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

def resolves(host):
    """Resolve a hostname, returning (ok, error).

    Used by check_dns_status to prove DNS works functionally: a cluster
    .xc name (split-DNS via /etc/resolver/xc over wg) and a public name
    must both resolve.
    """
    try:
        socket.getaddrinfo(host, None)
        return True, None
    except OSError as e:
        return False, str(e)

def check_ntp_status():
    """Check NTP configuration on macOS.

    Sync state is reported separately by check_clock_skew, which queries
    the same NTP server in-process via ntplib (and returns the actual
    offset_ms / stratum). Calling sntp here too would be a redundant
    ~800ms roundtrip per /api/health request on every macOS node.
    """
    try:
        # Check NTP server configuration
        ntp_server_result = subprocess.run(['systemsetup', '-getnetworktimeserver'],
                                         capture_output=True, text=True, timeout=2)

        # Check if automatic time setting is enabled
        auto_time_result = subprocess.run(['systemsetup', '-getusingnetworktime'],
                                        capture_output=True, text=True, timeout=2)

        ntp_server_configured = ntp_server_result.returncode == 0
        auto_time_enabled = auto_time_result.returncode == 0 and 'On' in auto_time_result.stdout

        # Parse "Network Time Server: 10.0.0.254" format
        ntp_server = 'unknown'
        if ntp_server_configured and ntp_server_result.stdout:
            lines = ntp_server_result.stdout.strip().split('\n')
            for line in lines:
                if 'Network Time Server:' in line:
                    ntp_server = line.split(':', 1)[1].strip()
                    break

        if ntp_server_configured and auto_time_enabled:
            status = 'healthy'
            message = f'NTP configured for {ntp_server}, auto time enabled'
        elif ntp_server_configured and not auto_time_enabled:
            status = 'degraded'
            message = 'NTP configured but auto time disabled'
        elif auto_time_enabled and not ntp_server_configured:
            status = 'degraded'
            message = 'Auto time enabled but NTP server not configured'
        else:
            status = 'unhealthy'
            message = 'NTP not configured'

        return {
            'status': status,
            'details': {
                'ntp_server': ntp_server,
                'auto_time_enabled': auto_time_enabled,
                'message': message,
            }
        }
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'NTP check failed: {str(e)}'}
        }


def check_dns_status():
    """Check DNS resolution on macOS — purely functional.

    Cluster names use split DNS (/etc/resolver/xc routes .xc to the cluster
    resolver over wg; the node's own resolver handles the rest), so rather than
    introspect how DNS is wired, just test what matters: that a cluster .xc name
    and a public name both resolve.
    """
    try:
        cluster_ok, cluster_err = resolves('s1.xc')
        public_ok, public_err = resolves('example.com')

        if cluster_ok and public_ok:
            status = 'healthy'
            message = 'DNS resolving (cluster + public)'
        elif cluster_ok:
            status = 'degraded'
            message = f'Cluster DNS ok but public resolution failed: {public_err}'
        else:
            status = 'unhealthy'
            message = f'Cluster .xc resolution failed: {cluster_err}'

        return {
            'status': status,
            'details': {
                'cluster_resolves': cluster_ok,
                'public_resolves': public_ok,
                'message': message,
            }
        }
    except Exception as e:
        return {
            'status': 'error',
            'details': {'message': f'DNS check failed: {str(e)}'}
        }

def check_network_connectivity():
    """Check network connectivity to cluster services.

    Probes run in parallel — sequential shell-outs took up to 6s when any
    of the 3 services was unreachable (each timeout=2). With 3 workers in
    parallel the worst case is ~2s, which keeps the macOS /api/health
    response comfortably under the 5s per-host cluster-status budget
    (the recent m3 flap root cause: network_connectivity alone spiked to
    1.6s, stacked on ntp's ~1s shell-outs to push the request past 3s).
    """
    from concurrent.futures import ThreadPoolExecutor
    cluster_services = {
        'gateway': ('10.0.0.254', 80),
        'storage': ('10.0.0.100', 5000),
        'dns': ('10.0.0.254', 53)
    }

    def _probe(service_name, host, port):
        return service_name, check_port_open(host, port, timeout=2)

    connectivity_status = {}
    overall_healthy = True

    with ThreadPoolExecutor(max_workers=len(cluster_services)) as ex:
        futures = [ex.submit(_probe, name, host, port)
                   for name, (host, port) in cluster_services.items()]
        for fut in futures:
            service_name, is_reachable = fut.result()
            host, port = cluster_services[service_name]
            connectivity_status[service_name] = {
                'host': host,
                'port': port,
                'reachable': is_reachable,
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
                              capture_output=True, text=True, timeout=2)
        
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
        
        # macOS nodes are never storage nodes and drift under native `timed`
        # between syncs, so use the looser non-storage band (warn 1s / crit 10s).
        # Keep in sync with ycluster.utils.clock_skew (not importable here).
        if abs(offset_ms) > 10000:  # More than 10 seconds
            status = 'critical'
        elif abs(offset_ms) > 1000:  # More than 1 second
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
    t_total = time.monotonic()
    log.info('request: start')

    health_status = {
        'overall': 'healthy',
        'services': {},
        'node_type': 'macos',
        'hostname': platform.node(),
        'platform': platform.platform()
    }

    def _timed(name, fn):
        """Run a check and log its elapsed time + resulting status."""
        t = time.monotonic()
        try:
            r = fn()
        except Exception as e:
            elapsed_ms = (time.monotonic() - t) * 1000
            log.warning(f'check {name}: crashed in {elapsed_ms:.1f}ms: {e}')
            raise
        elapsed_ms = (time.monotonic() - t) * 1000
        status = r.get('status') if isinstance(r, dict) else '?'
        log.info(f'check {name}: {elapsed_ms:.1f}ms status={status}')
        return r

    # Check NTP
    ntp_health = _timed('ntp', check_ntp_status)
    health_status['services']['ntp'] = ntp_health
    if ntp_health['status'] in ['unhealthy', 'critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif ntp_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    # Check DNS
    dns_health = _timed('dns', check_dns_status)
    health_status['services']['dns'] = dns_health
    if dns_health['status'] in ['unhealthy', 'critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif dns_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    # Check network connectivity
    network_health = _timed('network_connectivity', check_network_connectivity)
    health_status['services']['network_connectivity'] = network_health
    if network_health['status'] in ['unhealthy', 'critical']:
        health_status['overall'] = 'unhealthy'
    elif network_health['status'] in ['degraded', 'warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    # Check disk space
    disk_health = _timed('disk_space', check_disk_space)
    health_status['services']['disk_space'] = disk_health
    if disk_health['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif disk_health['status'] in ['warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    # Check system load
    load_health = _timed('system_load', check_system_load)
    health_status['services']['system_load'] = load_health
    if load_health['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif load_health['status'] in ['warning'] and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    # Check clock skew
    clock_skew = _timed('clock_skew', check_clock_skew)
    health_status['services']['clock_skew'] = clock_skew
    if clock_skew['status'] in ['critical', 'error']:
        health_status['overall'] = 'unhealthy'
    elif clock_skew['status'] == 'warning' and health_status['overall'] == 'healthy':
        health_status['overall'] = 'degraded'

    elapsed_total_ms = (time.monotonic() - t_total) * 1000
    log.info(f'request: done in {elapsed_total_ms:.1f}ms overall={health_status["overall"]}')

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
    # waitress matches what health-api.py uses on Linux nodes (storage,
    # compute, nvidia, nas): production WSGI server with thread pool.
    # Flask's dev server is single-threaded and not for production —
    # without this, concurrent requests on the cluster-status fan-out
    # queue up and the slowest one trips the 5s per-host timeout.
    from waitress import serve
    serve(app, host='0.0.0.0', port=12724, threads=8)
