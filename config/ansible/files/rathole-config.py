#!/usr/bin/env python3
"""
Script to manage rathole configuration in etcd
"""

import json
import sys
import argparse
import etcd3
import os
import socket
import re
from jinja2 import Template

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

def validate_host_port(remote_addr):
    """Validate that remote_addr is in host:port format"""
    if ':' not in remote_addr:
        raise ValueError(f"Invalid format: '{remote_addr}'. Must be host:port")
    
    host, port_str = remote_addr.rsplit(':', 1)
    if not host:
        raise ValueError(f"Invalid format: '{remote_addr}'. Host cannot be empty")
    
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid port: {port}. Must be 1-65535")
    except ValueError as e:
        if "invalid literal" in str(e):
            raise ValueError(f"Invalid port: '{port_str}'. Must be a number")
        raise

def set_rathole_config(remote_addr, token):
    """Set rathole configuration in etcd"""
    if remote_addr:
        validate_host_port(remote_addr)
    
    client = get_etcd_client()
    key = '/cluster/nodes/rathole/config'
    
    # Get existing config if updating partially
    config = {}
    if not (remote_addr and token):
        value, _ = client.get(key)
        if value:
            config = json.loads(value.decode())
    
    if remote_addr:
        config['remote_addr'] = remote_addr
    if token:
        config['token'] = token
    
    if not config.get('remote_addr') or not config.get('token'):
        print("Error: Both remote_addr and token must be set", file=sys.stderr)
        sys.exit(1)
    
    value = json.dumps(config)
    client.put(key, value)
    print(f"Set rathole config: remote_addr={config['remote_addr']}, token=***")

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

def generate_client_config():
    """Generate client configuration from etcd and output to stdout"""
    _generate_config('/etc/rathole/client-config.toml.j2')

def generate_ssh_client_config():
    """Generate SSH-only client configuration from etcd and output to stdout"""
    _generate_config('/etc/rathole/ssh-client-config.toml.j2')

def _generate_config(template_path):
    """Generate rathole configuration from etcd template"""
    client = get_etcd_client()
    key = '/cluster/nodes/rathole/config'
    
    value, _ = client.get(key)
    if not value:
        print("Error: No rathole configuration found in etcd", file=sys.stderr)
        sys.exit(1)
    
    config = json.loads(value.decode())
    remote_addr = config.get('remote_addr')
    token = config.get('token')
    
    if not remote_addr:
        print("Error: No remote_addr found in rathole configuration", file=sys.stderr)
        sys.exit(1)
    
    if not token:
        print("Error: No token found in rathole configuration", file=sys.stderr)
        sys.exit(1)
    
    # Determine core node index from hostname using regex
    hostname = socket.gethostname()
    match = re.match(r'^s([123])$', hostname)
    if not match:
        print(f"Error: Not a core node. Hostname '{hostname}' must be s1-s3", file=sys.stderr)
        sys.exit(1)
    
    idx = int(match.group(1))
    
    # Read template file and render with jinja2
    try:
        with open(template_path, 'r') as f:
            template_content = f.read()
        
        template = Template(template_content)
        client_config = template.render(remote_addr=remote_addr, token=token, idx=idx)
        print(client_config)
        
    except FileNotFoundError:
        print(f"Error: Template file {template_path} not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error rendering template: {e}", file=sys.stderr)
        sys.exit(1)

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
    set_parser.add_argument('--remote-addr', help='Remote server address (e.g., server.com:2333)')
    set_parser.add_argument('--token', help='Authentication token')
    
    # Get command
    subparsers.add_parser('get', help='Get current rathole configuration')
    
    # Generate client config command
    subparsers.add_parser('generate-client', help='Generate client configuration from etcd')
    
    # Generate SSH-only client config command
    subparsers.add_parser('generate-ssh-client', help='Generate SSH-only client configuration from etcd')
    
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
        elif args.command == 'generate-client':
            generate_client_config()
        elif args.command == 'generate-ssh-client':
            generate_ssh_client_config()
        elif args.command == 'delete':
            delete_rathole_config()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
