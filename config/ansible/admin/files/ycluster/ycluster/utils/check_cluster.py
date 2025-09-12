#!/usr/bin/python3
"""
Check etcd health across all cluster nodes and list stored node entries.
"""

import etcd3
import json
import sys
import os
import subprocess
import socket
import urllib.request
import urllib.error
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..common.etcd_utils import get_etcd_client, get_etcd_hosts

# Get initial etcd host from environment or use default
INITIAL_ETCD_HOST = get_etcd_hosts()[0]
ETCD_PREFIX = '/cluster/nodes'
DHCP_HEALTH_PORT = int(os.environ.get('DHCP_HEALTH_PORT', '8067'))

def get_cluster_members(initial_host):
    """Get all etcd cluster members using etcdctl"""
    try:
        import subprocess
        result = subprocess.run(['etcdctl', 'member', 'list', '--write-out=json'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            members_data = json.loads(result.stdout)
            members = []
            for member in members_data.get('members', []):
                for url in member.get('clientURLs', []):
                    if url.startswith('http://'):
                        ip = url.replace('http://', '').split(':')[0]
                        members.append(f"{ip}:2379")
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
        client = etcd3.client(host=host, port=int(port))
        
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
        is_leader = current_member_id == status.leader.id if current_member_id else False
        
        return {
            'host': host_port,
            'healthy': True,
            'version': status.version,
            'db_size': status.db_size,
            'leader_id': status.leader.id,
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
        with urllib.request.urlopen(url, timeout=5) as response:
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
    except (urllib.error.URLError, socket.timeout, Exception) as e:
        error_type = 'timeout' if isinstance(e, socket.timeout) else 'connection failed' if isinstance(e, urllib.error.URLError) else 'unexpected error'
        error_msg = 'Health endpoint timeout' if isinstance(e, socket.timeout) else str(e.reason if hasattr(e, 'reason') else e)
        return {'host': host_ip, 'running': False, 'error': f'{error_type.title()}: {error_msg}', 'method': 'health_endpoint'}

def check_dnsmasq_service_on_host(host_ip, hostname):
    """Check if DNS service is running on a specific host and can resolve hostname"""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [host_ip]
        resolver.timeout = 3
        resolver.lifetime = 3
        
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

def main():
    """Main function to check etcd cluster health"""
    print("=" * 60)
    print("ETCD Cluster Health Check")
    print("=" * 60)
    
    # Get cluster members dynamically
    etcd_hosts = get_cluster_members(INITIAL_ETCD_HOST)
    print(f"Discovered cluster members: {', '.join(etcd_hosts)}")
    print()
    
    # Check each etcd host in parallel
    health_results = []
    with ThreadPoolExecutor(max_workers=len(etcd_hosts)) as executor:
        future_to_host = {executor.submit(check_etcd_host, host): host 
                         for host in etcd_hosts}
        
        for future in as_completed(future_to_host):
            result = future.result()
            health_results.append(result)
    
    # Display health results
    print("Host Health Status:")
    print("-" * 60)
    for result in sorted(health_results, key=lambda x: x['host']):
        if result['healthy']:
            leader_str = " (LEADER)" if result.get('is_leader') else ""
            print(f"✓ {result['host']}: Healthy{leader_str}")
            print(f"  Version: {result['version']}, DB Size: {result['db_size']:,} bytes")
        else:
            print(f"✗ {result['host']}: Unhealthy - {result['error']}")
    
    # Get nodes from first healthy host for service checks
    try:
        client = get_etcd_client(etcd_hosts)
    except Exception:
        client = None

    if client:
        nodes = get_all_nodes(client)
        
        # Check DHCP service across cluster
        print("\n" + "=" * 60)
        print("DHCP Service Status:")
        print("-" * 60)
        
        dhcp_running_nodes = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            dhcp_futures = {executor.submit(check_dhcp_service_on_host, node['ip']): node 
                           for node in nodes if node.get('ip') and not node.get('hostname', '').endswith('a')}
            
            for future in as_completed(dhcp_futures):
                result = future.result()
                node = dhcp_futures[future]
                if result['running']:
                    dhcp_running_nodes.append(result['host'])
                    method = result.get('method', 'unknown')
                    etcd_status = "✓" if result.get('etcd_connected', False) else "✗"
                    print(f"✓ {node.get('hostname', 'unknown')} ({result['host']}): DHCP Running (via {method})")
                    print(f"  etcd connection: {etcd_status}, server IP: {result.get('server_ip', 'unknown')}")
                else:
                    print(f"✗ {node.get('hostname', 'unknown')} ({result['host']}): DHCP Not running - {result['error']}")
        
        if len(dhcp_running_nodes) == 0:
            print("⚠ WARNING: No DHCP servers running in cluster!")
        elif len(dhcp_running_nodes) > 1:
            print(f"⚠ WARNING: Multiple DHCP servers running: {', '.join(dhcp_running_nodes)}")
        else:
            print(f"✓ DHCP leader election working correctly - single server on {dhcp_running_nodes[0]}")
        
        # Check dnsmasq service across cluster
        print("\n" + "=" * 60)
        print("dnsmasq Service Status:")
        print("-" * 60)
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            dnsmasq_futures = {executor.submit(check_dnsmasq_service_on_host, node['ip'], node.get('hostname', 'unknown')): node 
                              for node in nodes if node.get('ip') and not node.get('hostname', '').endswith('a')}
            
            for future in as_completed(dnsmasq_futures):
                result = future.result()
                node = dnsmasq_futures[future]
                if result['running']:
                    method = result.get('method', 'unknown')
                    if result.get('dns_working', False):
                        print(f"✓ {node.get('hostname', 'unknown')} ({result['host']}): DNS Running (via {method})")
                        print(f"  DNS Resolution: {result['hostname']} -> {result['resolved_ip']}")
                        if 'amt_hostname' in result:
                            print(f"  AMT Resolution: {result['amt_hostname']} -> {result['amt_resolved_ip']}")
                    else:
                        print(f"⚠ {node.get('hostname', 'unknown')} ({result['host']}): DNS Running but resolution failed (via {method})")
                        print(f"  DNS Error: {result.get('dns_error', 'Unknown error')}")
                else:
                    print(f"✗ {node.get('hostname', 'unknown')} ({result['host']}): DNS Not running - {result['error']}")
    
    print("\n" + "=" * 60)
    print("Registered Nodes:")
    print("-" * 60)
    
    if client:
        if nodes:
            # Sort by hostname
            nodes.sort(key=lambda x: x.get('hostname', ''))
            
            print(f"{'Hostname':<10} {'Type':<10} {'IP':<15} {'MAC':<20} {'Allocated'}")
            print("-" * 80)
            for node in nodes:
                allocated = node.get('allocated_at', 'Unknown')
                if allocated != 'Unknown':
                    try:
                        dt = datetime.fromisoformat(allocated.replace('Z', '+00:00'))
                        allocated = dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        pass
                
                print(f"{node.get('hostname', 'N/A'):<10} "
                      f"{node.get('type', 'N/A'):<10} "
                      f"{node.get('ip', 'N/A'):<15} "
                      f"{node.get('mac', 'N/A'):<20} "
                      f"{allocated}")
        else:
            print("No nodes registered in etcd")
    else:
        print("Could not connect to any etcd host to retrieve nodes")
    
    print("=" * 60)

if __name__ == '__main__':
    # disable proxies
    del os.environ['http_proxy']
    del os.environ['https_proxy']
    main()
