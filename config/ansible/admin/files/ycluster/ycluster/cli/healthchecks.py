#!/usr/bin/env python3
"""
Healthchecks.io configuration management
"""

import sys
from ..common.etcd_utils import get_etcd_client

ETCD_KEY = '/cluster/healthchecks/url'

def register_healthchecks_commands(subparsers):
    """Register healthchecks subcommands"""
    healthchecks_parser = subparsers.add_parser(
        'healthchecks',
        help='Manage healthchecks.io configuration'
    )
    
    healthchecks_subparsers = healthchecks_parser.add_subparsers(
        dest='healthchecks_command',
        help='Healthchecks commands'
    )
    
    # Set URL
    set_parser = healthchecks_subparsers.add_parser(
        'set-url',
        help='Set healthchecks.io ping URL'
    )
    set_parser.add_argument(
        'url',
        help='Healthchecks.io ping URL (e.g., https://hc-ping.com/YOUR-UUID-HERE)'
    )
    set_parser.set_defaults(func=healthchecks_set_url)
    
    # Get URL
    get_parser = healthchecks_subparsers.add_parser(
        'get-url',
        help='Get current healthchecks.io ping URL'
    )
    get_parser.set_defaults(func=healthchecks_get_url)
    
    # Delete URL
    delete_parser = healthchecks_subparsers.add_parser(
        'delete-url',
        help='Delete healthchecks.io ping URL'
    )
    delete_parser.set_defaults(func=healthchecks_delete_url)
    
    # Test ping
    test_parser = healthchecks_subparsers.add_parser(
        'test',
        help='Send a test ping to healthchecks.io'
    )
    test_parser.set_defaults(func=healthchecks_test)

def healthchecks_set_url(args):
    """Set healthchecks.io ping URL in etcd"""
    try:
        client = get_etcd_client()
        
        # Validate URL format
        url = args.url.strip()
        if not url.startswith(('http://', 'https://')):
            print(f"Error: URL must start with http:// or https://", file=sys.stderr)
            sys.exit(1)
        
        # Store in etcd
        client.put(ETCD_KEY, url)
        print(f"Healthchecks URL set to: {url}")
        
    except Exception as e:
        print(f"Error setting healthchecks URL: {e}", file=sys.stderr)
        sys.exit(1)

def healthchecks_get_url(args):
    """Get current healthchecks.io ping URL from etcd"""
    try:
        client = get_etcd_client()
        
        result = client.get(ETCD_KEY)
        if result[0]:
            url = result[0].decode()
            print(f"Healthchecks URL: {url}")
        else:
            print("No healthchecks URL configured")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error getting healthchecks URL: {e}", file=sys.stderr)
        sys.exit(1)

def healthchecks_delete_url(args):
    """Delete healthchecks.io ping URL from etcd"""
    try:
        client = get_etcd_client()
        
        # Check if exists
        result = client.get(ETCD_KEY)
        if not result[0]:
            print("No healthchecks URL configured")
            sys.exit(1)
        
        # Delete from etcd
        client.delete(ETCD_KEY)
        print("Healthchecks URL deleted")
        
    except Exception as e:
        print(f"Error deleting healthchecks URL: {e}", file=sys.stderr)
        sys.exit(1)

def healthchecks_test(args):
    """Send a test ping to healthchecks.io"""
    import requests
    import socket
    from datetime import datetime
    
    try:
        client = get_etcd_client()
        
        # Get URL from etcd
        result = client.get(ETCD_KEY)
        if not result[0]:
            print("No healthchecks URL configured", file=sys.stderr)
            sys.exit(1)
        
        url = result[0].decode()
        
        # Prepare test message
        hostname = socket.gethostname()
        message = f"Test ping from {hostname} at {datetime.now().isoformat()}"
        
        # Send ping
        print(f"Sending test ping to: {url}")
        response = requests.post(url, data=message, timeout=10)
        
        if response.status_code == 200:
            print("Test ping sent successfully")
        else:
            print(f"Test ping failed with HTTP {response.status_code}", file=sys.stderr)
            sys.exit(1)
            
    except requests.exceptions.RequestException as e:
        print(f"Error sending test ping: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
