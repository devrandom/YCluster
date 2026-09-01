#!/usr/bin/python3
"""
Check etcd health across all cluster nodes and list stored node entries.
"""

import etcd3
import json
import re
import sys
import os
import subprocess
import socket
import urllib.request
import urllib.error
from typing import Any

from ..common.etcd_utils import get_etcd_client, get_etcd_hosts, get_tls_kwargs

# Disable gRPC HTTP proxy to avoid routing etcd connections through squid
GRPC_OPTIONS = [('grpc.enable_http_proxy', 0)]

# TLS material from the environment (empty dict = plaintext). When set, the
# direct etcd3 clients below present the client cert, and etcdctl gets https
# endpoints (it picks up ETCDCTL_CACERT/CERT/KEY from the environment).
_TLS_KWARGS = get_tls_kwargs()
_ETCD_SCHEME = 'https' if _TLS_KWARGS else 'http'

# Get initial etcd host from environment or use default
INITIAL_ETCD_HOST = get_etcd_hosts()[0]
ETCD_PREFIX = '/cluster/nodes'
DHCP_HEALTH_PORT = int(os.environ.get('DHCP_HEALTH_PORT', '8067'))
STORAGE_VIP = '10.0.0.100'
ADMIN_API_PORT = 12723

def get_cluster_members(initial_host):
    """Get all etcd cluster members using etcdctl"""
    try:
        endpoints = ','.join(f"{_ETCD_SCHEME}://{h}" for h in get_etcd_hosts())
        # Pass endpoints via the flag; drop ETCDCTL_ENDPOINTS from the child env
        # so etcdctl doesn't abort on "env var shadowed by command-line flag".
        # Cert vars (ETCDCTL_CACERT/CERT/KEY) are kept for TLS.
        child_env = {k: v for k, v in os.environ.items() if k != 'ETCDCTL_ENDPOINTS'}
        result = subprocess.run(['etcdctl', '--endpoints', endpoints, 'member', 'list', '--write-out=json'],
                              capture_output=True, text=True, timeout=5, env=child_env)
        if result.returncode == 0:
            members_data = json.loads(result.stdout)
            members = []
            for member in members_data.get('members', []):
                for url in member.get('clientURLs', []):
                    if '://' in url:
                        # Keep the advertised host:port (the client port varies
                        # by TLS phase — plaintext vs the dedicated TLS port).
                        members.append(url.split('://', 1)[1])
                        break
            if members:
                return members
    except Exception as e:
        print(f"Error using etcdctl: {e}")
    
    # Fallback to initial host
    return [initial_host]

def check_etcd_host(host_port):
    """Check health of a single etcd host"""
    try:
        host, port = host_port.split(':')
        client = etcd3.client(host=host, port=int(port), grpc_options=GRPC_OPTIONS, **_TLS_KWARGS)

        # Get status
        status = client.status()
        
        # Get member list and find current member
        members = list(client.members)
        current_member_id = None
        
        # Find the member ID for the current host we're connected to
        for member in members:
            for url in member.client_urls:
                if host in url:
                    current_member_id = member.id
                    break
            if current_member_id:
                break
        
        # Check if this specific member is the leader
        leader_id = status.leader.id if status.leader else None
        is_leader = current_member_id == leader_id if current_member_id else False

        return {
            'host': host_port,
            'healthy': True,
            'version': status.version,
            'db_size': status.db_size,
            'leader_id': leader_id,
            'is_leader': is_leader,
            'raft_index': status.raft_index,
            'member_count': len(members)
        }
    except Exception as e:
        return {
            'host': host_port,
            'healthy': False,
            'error': str(e)
        }

def get_all_nodes(client):
    """Get all nodes from etcd"""
    nodes = []
    try:
        # Get all keys with prefix
        for value, metadata in client.get_prefix(f"{ETCD_PREFIX}/by-hostname/"):
            try:
                node_data = json.loads(value.decode('utf-8'))
                nodes.append(node_data)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        print(f"Error getting nodes: {e}")
    
    return nodes

def check_dhcp_service_on_host(host_ip):
    """Check if DHCP server is running on a specific host using health monitoring endpoint"""
    try:
        url = f"http://{host_ip}:{DHCP_HEALTH_PORT}/health"
        with urllib.request.urlopen(url, timeout=1) as response:
            health_data = json.loads(response.read().decode())
            
            if response.status == 200 and health_data.get('status') == 'healthy':
                return {
                    'host': host_ip, 'running': True, 'method': 'health_endpoint',
                    'etcd_connected': health_data.get('etcd_connected', False),
                    'server_ip': health_data.get('server_ip', host_ip)
                }
            else:
                status = health_data.get('status', f'HTTP {response.status}')
                return {
                    'host': host_ip, 'running': False, 'method': 'health_endpoint',
                    'error': f'DHCP server unhealthy: {status}'
                }
                
    except urllib.error.HTTPError as e:
        error = 'Health endpoint not found (DHCP server may not be running)' if e.code == 404 else f'HTTP error {e.code}: {e.reason}'
        return {'host': host_ip, 'running': False, 'error': error, 'method': 'health_endpoint'}
    except urllib.error.URLError as e:
        return {'host': host_ip, 'running': False, 'error': f'Connection failed: {e.reason}', 'method': 'health_endpoint'}
    except socket.timeout:
        return {'host': host_ip, 'running': False, 'error': 'Health endpoint timeout', 'method': 'health_endpoint'}
    except Exception as e:
        return {'host': host_ip, 'running': False, 'error': f'Unexpected error: {e}', 'method': 'health_endpoint'}

def check_dnsmasq_service_on_host(host_ip, hostname):
    """Check if DNS service is running on a specific host and can resolve hostname"""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [host_ip]
        resolver.timeout = 1
        resolver.lifetime = 1
        
        # Try to resolve the hostname and its AMT counterpart
        results = {'host': host_ip, 'hostname': hostname, 'running': True, 'dns_working': True, 'method': 'dns_query'}
        
        try:
            # Test main hostname
            answer = resolver.resolve(hostname, 'A')
            resolved_ip = str(answer[0])
            results['resolved_ip'] = resolved_ip
            
            # Test AMT hostname if this is a regular node (not already AMT)
            if not hostname.endswith('a'):
                amt_hostname = f"{hostname}a"
                try:
                    amt_answer = resolver.resolve(amt_hostname, 'A')
                    amt_resolved_ip = str(amt_answer[0])
                    results['amt_hostname'] = amt_hostname
                    results['amt_resolved_ip'] = amt_resolved_ip
                    
                    # Verify AMT IP is in correct subnet
                    if not amt_resolved_ip.startswith('10.10.10.'):
                        results['dns_working'] = False
                        results['dns_error'] = f'AMT hostname {amt_hostname} resolved to wrong subnet: {amt_resolved_ip} (expected 10.10.10.x)'
                except dns.resolver.NXDOMAIN:
                    results['dns_working'] = False
                    results['dns_error'] = f'AMT hostname {amt_hostname} not found in DNS'
                except Exception as amt_e:
                    results['dns_working'] = False
                    results['dns_error'] = f'AMT DNS query failed: {amt_e}'
            
            return results
            
        except dns.resolver.NXDOMAIN:
            return {
                'host': host_ip,
                'hostname': hostname,
                'running': True,
                'dns_working': False,
                'dns_error': f'Hostname {hostname} not found in DNS',
                'method': 'dns_query'
            }
        except dns.resolver.NoNameservers:
            return {
                'host': host_ip,
                'hostname': hostname,
                'running': False,
                'error': 'DNS server not responding',
                'method': 'dns_query'
            }
        except Exception as dns_e:
            return {
                'host': host_ip,
                'hostname': hostname,
                'running': False,
                'error': f'DNS query failed: {dns_e}',
                'method': 'dns_query'
            }
                
    except ImportError:
        return {
            'host': host_ip, 
            'hostname': hostname, 
            'running': False, 
            'error': 'dnspython not installed'
        }
    except Exception as e:
        return {'host': host_ip, 'hostname': hostname, 'running': False, 'error': str(e)}


def fetch_cluster_status() -> dict[str, Any]:
    """Fetch aggregated cluster status from the storage VIP admin API."""
    try:
        url = f"http://{STORAGE_VIP}:{ADMIN_API_PORT}/api/cluster-status"
        with urllib.request.urlopen(url, timeout=15) as response:
            data: dict[str, Any] = json.loads(response.read().decode())
            return data
    except Exception as e:
        return {'_error': str(e)}


def _svc_status_str(details: Any) -> str:
    """Extract a human-readable string from a service details field."""
    if isinstance(details, dict):
        if 'message' in details:
            return details['message']
        # Construct summary from common service detail fields
        parts = []
        if details.get('service_active'):
            parts.append('active')
        if details.get('port_open'):
            parts.append('port open')
        if parts:
            return ', '.join(parts)
        return str(details)
    return str(details) if details else ''


def _d(val: Any) -> dict[str, Any]:
    """Ensure a value from JSON is treated as a dict (for type checker)."""
    return val if isinstance(val, dict) else {}


def _make_marker(enabled: bool):
    """Return a ``mark(label)`` callable that records phase boundaries.

    When ``enabled`` is False the returned callable is a no-op so callers
    can call it unconditionally without paying the dict-store overhead.
    """
    if not enabled:
        return lambda label: None
    import time
    timings: list[tuple[str, float]] = []
    t0 = [time.monotonic()]
    last = [t0[0]]

    def mark(label: str) -> None:
        now = time.monotonic()
        timings.append((label, (now - last[0]) * 1000.0))
        last[0] = now

    mark._total_ms = lambda: (time.monotonic() - t0[0]) * 1000.0  # ty: ignore[unresolved-attribute]
    mark._timings = timings  # ty: ignore[unresolved-attribute]
    return mark


def _print_profile(mark) -> None:
    """Print collected phase timings to stderr."""
    timings = mark._timings
    total = mark._total_ms()
    print('\n[profile] phase timings (ms):', file=sys.stderr)
    for label, ms in timings:
        print(f'  {label:<28} {ms:>8.1f}', file=sys.stderr)
    print(f'  {"TOTAL":<28} {total:>8.1f}', file=sys.stderr)


def main(verbose=False, profile=False):
    """Main entry point.  Prints results as they are collected."""
    os.environ.pop('http_proxy', None)
    os.environ.pop('https_proxy', None)

    mark = _make_marker(profile)
    mark('start')

    from .check_cluster_views import _run_compact, _run_verbose

    if verbose:
        _run_verbose(mark=mark)
    else:
        _run_compact(mark=mark)

    mark('total')
    if profile:
        _print_profile(mark)



if __name__ == '__main__':
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    main(verbose=verbose)
