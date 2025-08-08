#!/usr/bin/python3
"""
Check etcd health across all cluster nodes and list stored node entries.
"""

import etcd3
import json
import sys
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Get initial etcd host from environment or use default
INITIAL_ETCD_HOST = os.environ.get('ETCD_HOST', 'localhost:2379')
ETCD_PREFIX = '/cluster/nodes'

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
    
    # Get nodes from first healthy host
    print("\n" + "=" * 60)
    print("Registered Nodes:")
    print("-" * 60)
    
    client = None
    for host_port in etcd_hosts:
        try:
            host, port = host_port.split(':')
            client = etcd3.client(host=host, port=int(port))
            client.status()  # Test connection
            break
        except:
            continue
    
    if client:
        nodes = get_all_nodes(client)
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
    main()
