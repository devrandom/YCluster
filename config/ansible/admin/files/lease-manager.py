#!/usr/bin/env python3
"""
DHCP Lease Manager - List and delete lease data from etcd
"""

import os
import sys
import json
import argparse
import etcd3
from datetime import datetime

# Configuration
ETCD_HOSTS = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')

def get_etcd_client():
    """Get etcd client with failover"""
    for host in ETCD_HOSTS:
        try:
            host_port = host.replace('http://', '').replace('https://', '')
            if ':' in host_port:
                host_ip, port = host_port.split(':')
                port = int(port)
            else:
                host_ip, port = host_port, 2379
            
            client = etcd3.client(host=host_ip, port=port, timeout=5)
            client.status()  # Test connection
            print(f"Connected to etcd at {host}")
            return client
        except Exception as e:
            print(f"Failed to connect to etcd at {host}: {e}")
            continue
    
    print("ERROR: Could not connect to any etcd host")
    return None

def list_allocations():
    """List all node allocations from etcd"""
    client = get_etcd_client()
    if not client:
        return
    
    print("\n=== Node Allocations ===")
    allocations = []
    
    # Get all allocations by hostname
    for value, metadata in client.get_prefix("/cluster/nodes/by-hostname/"):
        if value:
            try:
                allocation = json.loads(value.decode())
                hostname = metadata.key.decode().split('/')[-1]
                allocations.append((hostname, allocation))
            except Exception as e:
                print(f"Error parsing allocation: {e}")
    
    # Sort by hostname
    allocations.sort(key=lambda x: x[0])
    
    if not allocations:
        print("No allocations found")
        return
    
    print(f"{'Hostname':<15} {'Type':<8} {'IP':<15} {'MAC':<17} {'Allocated At'}")
    print("-" * 80)
    
    for hostname, allocation in allocations:
        allocated_at = allocation.get('allocated_at', 'Unknown')
        if allocated_at != 'Unknown':
            try:
                # Parse and format timestamp
                dt = datetime.fromisoformat(allocated_at.replace('Z', '+00:00'))
                allocated_at = dt.strftime('%Y-%m-%d %H:%M:%S')
            except:
                pass
        
        print(f"{hostname:<15} {allocation.get('type', 'unknown'):<8} {allocation.get('ip', 'unknown'):<15} {allocation.get('mac', 'unknown'):<17} {allocated_at}")

def list_leases():
    """List all DHCP leases from etcd"""
    client = get_etcd_client()
    if not client:
        return
    
    print("\n=== DHCP Leases ===")
    leases = []
    
    # Get all leases
    for value, metadata in client.get_prefix("/cluster/dhcp/leases/"):
        if value:
            try:
                lease = json.loads(value.decode())
                mac = metadata.key.decode().split('/')[-1]
                leases.append((mac, lease))
            except Exception as e:
                print(f"Error parsing lease: {e}")
    
    # Sort by MAC
    leases.sort(key=lambda x: x[0])
    
    if not leases:
        print("No leases found")
        return
    
    print(f"{'MAC Address':<17} {'IP':<15} {'Hostname':<15} {'Expires':<19} {'Status'}")
    print("-" * 85)
    
    now = datetime.now()
    for mac_key, lease in leases:
        expires_str = lease.get('expires', 'Unknown')
        status = 'Unknown'
        
        if expires_str != 'Unknown':
            try:
                expires = datetime.fromisoformat(expires_str)
                expires_str = expires.strftime('%Y-%m-%d %H:%M:%S')
                status = 'Active' if expires > now else 'Expired'
            except:
                pass
        
        hostname = lease.get('hostname', '')
        ip = lease.get('ip', 'unknown')
        
        # Display the MAC from lease data (with colons) if available, otherwise use key
        display_mac = lease.get('mac', mac_key)
        print(f"{display_mac:<17} {ip:<15} {hostname:<15} {expires_str:<19} {status}")

def delete_by_hostname(hostname):
    """Delete all etcd entries related to a hostname"""
    client = get_etcd_client()
    if not client:
        return False
    
    print(f"Deleting all entries for hostname: {hostname}")
    
    # Get the allocation to find the MAC address
    allocation_data = client.get(f"/cluster/nodes/by-hostname/{hostname}")
    if not allocation_data[0]:
        print(f"No allocation found for hostname: {hostname}")
        return False
    
    try:
        allocation = json.loads(allocation_data[0].decode())
        mac = allocation['mac']
        # Normalize MAC to lowercase without separators for etcd keys
        normalized_mac = mac.lower().replace(':', '').replace('-', '')
        ip = allocation['ip']
        
        print(f"Found allocation: {hostname} -> {ip} (MAC: {mac})")
        
        keys_to_delete = [
            f"/cluster/nodes/by-hostname/{hostname}",
            f"/cluster/nodes/by-mac/{normalized_mac}",
            f"/cluster/dhcp/leases/{normalized_mac}"
        ]
        
        deleted_count = 0
        for key in keys_to_delete:
            try:
                result = client.delete(key)
                if result:
                    print(f"Deleted: {key}")
                    deleted_count += 1
                else:
                    print(f"Not found: {key}")
            except Exception as e:
                print(f"Error deleting {key}: {e}")
        
        print(f"Successfully deleted {deleted_count} entries for {hostname}")
        return deleted_count > 0
        
    except Exception as e:
        print(f"Error processing allocation data: {e}")
        return False

def delete_by_mac(mac):
    """Delete all etcd entries related to a MAC address"""
    client = get_etcd_client()
    if not client:
        return False
    
    # Normalize MAC address
    normalized_mac = mac.lower().replace(':', '').replace('-', '')
    
    print(f"Deleting all entries for MAC: {mac} (normalized: {normalized_mac})")
    
    # Get the allocation to find the hostname
    allocation_data = client.get(f"/cluster/nodes/by-mac/{normalized_mac}")
    if not allocation_data[0]:
        print(f"No allocation found for MAC: {mac}")
        # Still try to delete lease entry using normalized MAC
        try:
            result = client.delete(f"/cluster/dhcp/leases/{normalized_mac}")
            if result:
                print(f"Deleted lease entry for MAC: {mac}")
                return True
            else:
                print(f"No lease found for MAC: {mac}")
                return False
        except Exception as e:
            print(f"Error deleting lease: {e}")
            return False
    
    try:
        allocation = json.loads(allocation_data[0].decode())
        hostname = allocation['hostname']
        ip = allocation['ip']
        
        print(f"Found allocation: {hostname} -> {ip} (MAC: {mac})")
        
        # Delete all related entries
        keys_to_delete = [
            f"/cluster/nodes/by-hostname/{hostname}",
            f"/cluster/nodes/by-mac/{normalized_mac}",
            f"/cluster/dhcp/leases/{normalized_mac}"
        ]
        
        deleted_count = 0
        for key in keys_to_delete:
            try:
                result = client.delete(key)
                if result:
                    print(f"Deleted: {key}")
                    deleted_count += 1
                else:
                    print(f"Not found: {key}")
            except Exception as e:
                print(f"Error deleting {key}: {e}")
        
        print(f"Successfully deleted {deleted_count} entries for MAC {mac}")
        return deleted_count > 0
        
    except Exception as e:
        print(f"Error processing allocation data: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description='DHCP Lease Manager')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List commands
    subparsers.add_parser('allocations', help='List all node allocations')
    subparsers.add_parser('leases', help='List all DHCP leases')
    subparsers.add_parser('all', help='List both allocations and leases')
    
    # Delete commands
    delete_parser = subparsers.add_parser('delete', help='Delete entries')
    delete_parser.add_argument('target', help='Hostname or MAC address to delete')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Unset proxy environment variables
    os.environ.pop('http_proxy', None)
    os.environ.pop('https_proxy', None)
    
    if args.command == 'all':
        list_allocations()
        list_leases()
    elif args.command == 'allocations':
        list_allocations()
    elif args.command == 'leases':
        list_leases()
    elif args.command == 'delete':
        # Auto-detect if target is hostname or MAC address
        target = args.target
        if ':' in target or len(target.replace(':', '').replace('-', '')) == 12:
            # Looks like a MAC address
            success = delete_by_mac(target)
        else:
            # Assume it's a hostname
            success = delete_by_hostname(target)
        
        if success:
            print("Deletion completed successfully")
        else:
            print("Deletion failed")
            sys.exit(1)

if __name__ == '__main__':
    main()
