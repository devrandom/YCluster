#!/usr/bin/env python3
"""
Script to manage frp configuration in etcd
"""

import json
import sys
import argparse
import socket
import re
from jinja2 import Template

from ..common.etcd_utils import get_etcd_client

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

def _merged_frp_config(client, remote_addr, token):
    """Merge partial updates with the existing etcd config."""
    config = {}
    if not (remote_addr and token):
        value, _ = client.get('/cluster/nodes/frp/config')
        if value:
            config = json.loads(value.decode())
    if remote_addr:
        config['remote_addr'] = remote_addr
    if token:
        config['token'] = token
    return config


def set_frp_config(remote_addr, token):
    """Set frp configuration in etcd"""
    if remote_addr:
        validate_host_port(remote_addr)

    client = get_etcd_client()
    config = _merged_frp_config(client, remote_addr, token)

    if not config.get('remote_addr') or not config.get('token'):
        print("Error: Both remote_addr and token must be set", file=sys.stderr)
        sys.exit(1)

    client.put('/cluster/nodes/frp/config', json.dumps(config))
    print(f"Set frp config: remote_addr={config['remote_addr']}, token=***")

def get_frp_config():
    """Get frp configuration from etcd"""
    client = get_etcd_client()
    key = '/cluster/nodes/frp/config'

    value, _ = client.get(key)
    if value:
        config = json.loads(value.decode())
        print(f"Remote address: {config.get('remote_addr', 'Not set')}")
        print(f"Token: {'***' if config.get('token') else 'Not set'}")
    else:
        print("No frp configuration found in etcd")

def generate_client_config():
    """Generate client configuration from etcd and output to stdout"""
    _generate_config('/etc/frp/client-config.toml.j2')

def generate_ssh_client_config():
    """Generate SSH-only client configuration from etcd and output to stdout"""
    _generate_config('/etc/frp/ssh-client-config.toml.j2')

def _generate_config(template_path):
    """Generate frp configuration from etcd template"""
    client = get_etcd_client()
    key = '/cluster/nodes/frp/config'

    value, _ = client.get(key)
    if not value:
        print("Error: No frp configuration found in etcd", file=sys.stderr)
        sys.exit(1)

    config = json.loads(value.decode())
    remote_addr = config.get('remote_addr')
    token = config.get('token')

    if not remote_addr:
        print("Error: No remote_addr found in frp configuration", file=sys.stderr)
        sys.exit(1)

    if not token:
        print("Error: No token found in frp configuration", file=sys.stderr)
        sys.exit(1)

    # Determine core node index from hostname. idx N maps to the server's
    # ssh{N} endpoint (port 17200+N), one per core node (s1, s2, ... s4+).
    hostname = socket.gethostname()
    match = re.match(r'^s(\d+)$', hostname)
    if not match:
        print(f"Error: Not a core node. Hostname '{hostname}' must be s<N>", file=sys.stderr)
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

def delete_frp_config():
    """Delete frp configuration from etcd"""
    client = get_etcd_client()
    key = '/cluster/nodes/frp/config'

    deleted = client.delete(key)
    if deleted:
        print("frp configuration deleted from etcd")
    else:
        print("No frp configuration found to delete")

def _build_parser():
    parser = argparse.ArgumentParser(description='Manage frp configuration in etcd')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    set_parser = subparsers.add_parser('set', help='Set frp configuration')
    set_parser.add_argument('--remote-addr', help='Remote server address (e.g., server.com:7000)')
    set_parser.add_argument('--token', help='Authentication token')

    subparsers.add_parser('get', help='Get current frp configuration')
    subparsers.add_parser('generate-client', help='Generate client configuration from etcd')
    subparsers.add_parser('generate-ssh-client', help='Generate SSH-only client configuration from etcd')
    subparsers.add_parser('delete', help='Delete frp configuration')
    return parser


def _run_command(command, args):
    handlers = {
        'set': lambda: set_frp_config(args.remote_addr, args.token),
        'get': get_frp_config,
        'generate-client': generate_client_config,
        'generate-ssh-client': generate_ssh_client_config,
        'delete': delete_frp_config,
    }
    handlers[command]()


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        _run_command(args.command, args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
