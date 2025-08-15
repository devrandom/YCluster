#!/usr/bin/env python3
"""
Script to manage rathole configuration in etcd
"""

import json
import sys
import argparse
import etcd3
import os

def get_etcd_client():
    """Get etcd client with connection to available hosts"""
    etcd_hosts = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')
    grpc_options = [('grpc.enable_http_proxy', 0)]
    
    for host_port in etcd_hosts:
        try:
            host, port = host_port.split(':')
            client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
            client.status()  # Test connection
            return client
        except Exception as e:
            print(f"Failed to connect to {host_port}: {e}")
            continue
    
    raise Exception(f"Could not connect to any etcd host: {etcd_hosts}")

def set_rathole_config(remote_addr, token):
    """Set rathole configuration in etcd"""
    client = get_etcd_client()
    config = {
        'remote_addr': remote_addr,
        'token': token
    }
    
    key = '/cluster/nodes/rathole/config'
    value = json.dumps(config)
    
    client.put(key, value)
    print(f"Set rathole config: remote_addr={remote_addr}, token=***")

def get_rathole_config():
    """Get rathole configuration from etcd"""
    client = get_etcd_client()
    key = '/cluster/nodes/rathole/config'
    
    value, _ = client.get(key)
    if value:
        config = json.loads(value.decode())
        print(f"Remote address: {config.get('remote_addr', 'Not set')}")
        print(f"Token: {'***' if config.get('token') else 'Not set'}")
    else:
        print("No rathole configuration found in etcd")

def delete_rathole_config():
    """Delete rathole configuration from etcd"""
    client = get_etcd_client()
    key = '/cluster/nodes/rathole/config'
    
    deleted = client.delete(key)
    if deleted:
        print("Rathole configuration deleted from etcd")
    else:
        print("No rathole configuration found to delete")

def main():
    parser = argparse.ArgumentParser(description='Manage rathole configuration in etcd')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Set command
    set_parser = subparsers.add_parser('set', help='Set rathole configuration')
    set_parser.add_argument('--remote-addr', required=True, help='Remote server address (e.g., server.com:2333)')
    set_parser.add_argument('--token', required=True, help='Authentication token')
    
    # Get command
    subparsers.add_parser('get', help='Get current rathole configuration')
    
    # Delete command
    subparsers.add_parser('delete', help='Delete rathole configuration')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'set':
            set_rathole_config(args.remote_addr, args.token)
        elif args.command == 'get':
            get_rathole_config()
        elif args.command == 'delete':
            delete_rathole_config()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
