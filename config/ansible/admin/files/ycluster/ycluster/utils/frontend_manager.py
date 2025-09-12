#!/usr/bin/env python3

import argparse
import json
from datetime import datetime

from ..common.etcd_utils import get_etcd_client

ETCD_PREFIX = '/cluster/nodes/frontend'

def list_frontend_nodes():
    """List all frontend nodes"""
    client = get_etcd_client()
    
    nodes = []
    for value, metadata in client.get_prefix(ETCD_PREFIX):
        if value:
            try:
                node_data = json.loads(value.decode('utf-8'))
                nodes.append(node_data)
            except json.JSONDecodeError:
                continue
    
    if not nodes:
        print("No frontend nodes found")
        return
    
    print(f"{'Name':<20} {'Address':<25} {'Added':<20}")
    print("-" * 65)
    for node in sorted(nodes, key=lambda x: x.get('name', '')):
        name = node.get('name', 'N/A')
        address = node.get('ip') or node.get('hostname', 'N/A')
        added = node.get('added', 'N/A')
        print(f"{name:<20} {address:<25} {added:<20}")

def add_frontend_node(name, address, description=None):
    """Add a frontend node"""
    client = get_etcd_client()
    
    # Check if node already exists
    key = f"{ETCD_PREFIX}/{name}"
    existing, _ = client.get(key)
    if existing:
        print(f"Frontend node '{name}' already exists")
        return False
    
    # Determine if address is IP or hostname
    import ipaddress
    try:
        ipaddress.ip_address(address)
        node_data = {
            'name': name,
            'ip': address,
            'type': 'frontend',
            'added': datetime.now().isoformat()
        }
    except (ipaddress.AddressValueError, ValueError):
        node_data = {
            'name': name,
            'hostname': address,
            'type': 'frontend',
            'added': datetime.now().isoformat()
        }
    
    if description:
        node_data['description'] = description
    
    # Store in etcd
    client.put(key, json.dumps(node_data))
    print(f"Added frontend node '{name}' at {address}")
    return True

def delete_frontend_node(name):
    """Delete a frontend node"""
    client = get_etcd_client()
    
    key = f"{ETCD_PREFIX}/{name}"
    existing, _ = client.get(key)
    if not existing:
        print(f"Frontend node '{name}' not found")
        return False
    
    client.delete(key)
    print(f"Deleted frontend node '{name}'")
    return True

def show_frontend_node(name):
    """Show details of a frontend node"""
    client = get_etcd_client()
    
    key = f"{ETCD_PREFIX}/{name}"
    value, _ = client.get(key)
    if not value:
        print(f"Frontend node '{name}' not found")
        return False
    
    try:
        node_data = json.loads(value.decode('utf-8'))
        print(f"Frontend Node: {name}")
        print("-" * 40)
        for key, val in node_data.items():
            print(f"{key.capitalize()}: {val}")
    except json.JSONDecodeError:
        print(f"Invalid data for frontend node '{name}'")
        return False
    
    return True

def main():
    parser = argparse.ArgumentParser(description='Manage frontend nodes in etcd')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List command
    subparsers.add_parser('list', help='List all frontend nodes')
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a frontend node')
    add_parser.add_argument('name', help='Node name')
    add_parser.add_argument('address', help='IP address or hostname')
    add_parser.add_argument('--description', help='Optional description')
    
    # Delete command
    del_parser = subparsers.add_parser('delete', help='Delete a frontend node')
    del_parser.add_argument('name', help='Node name to delete')
    
    # Show command
    show_parser = subparsers.add_parser('show', help='Show frontend node details')
    show_parser.add_argument('name', help='Node name to show')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == 'list':
        list_frontend_nodes()
    elif args.command == 'add':
        add_frontend_node(args.name, args.address, args.description)
    elif args.command == 'delete':
        delete_frontend_node(args.name)
    elif args.command == 'show':
        show_frontend_node(args.name)

if __name__ == '__main__':
    main()
