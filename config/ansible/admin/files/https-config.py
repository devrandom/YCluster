#!/usr/bin/env python3
"""
Script to manage HTTPS domain configuration in etcd
"""

import json
import sys
import argparse

from common.etcd_utils import get_etcd_client

def get_https_config():
    """Get HTTPS configuration from etcd"""
    client = get_etcd_client()
    
    domain_key = '/cluster/https/domain'
    aliases_key = '/cluster/https/aliases'
    email_key = '/cluster/https/email'
    
    domain_value, _ = client.get(domain_key)
    aliases_value, _ = client.get(aliases_key)
    email_value, _ = client.get(email_key)
    
    config = {}
    
    if domain_value:
        config['domain'] = domain_value.decode().strip()
    
    if aliases_value:
        try:
            config['aliases'] = json.loads(aliases_value.decode())
        except json.JSONDecodeError:
            config['aliases'] = []
    else:
        config['aliases'] = []
    
    if email_value:
        config['email'] = email_value.decode().strip()
    
    return config

def set_domain(domain):
    """Set primary domain in etcd"""
    client = get_etcd_client()
    key = '/cluster/https/domain'
    client.put(key, domain)
    print(f"Set primary domain: {domain}")

def add_alias(alias):
    """Add domain alias to etcd"""
    client = get_etcd_client()
    key = '/cluster/https/aliases'
    
    # Get existing aliases
    value, _ = client.get(key)
    if value:
        try:
            aliases = json.loads(value.decode())
        except json.JSONDecodeError:
            aliases = []
    else:
        aliases = []
    
    if alias not in aliases:
        aliases.append(alias)
        client.put(key, json.dumps(aliases))
        print(f"Added alias: {alias}")
    else:
        print(f"Alias already exists: {alias}")

def remove_alias(alias):
    """Remove domain alias from etcd"""
    client = get_etcd_client()
    key = '/cluster/https/aliases'
    
    # Get existing aliases
    value, _ = client.get(key)
    if value:
        try:
            aliases = json.loads(value.decode())
        except json.JSONDecodeError:
            aliases = []
    else:
        aliases = []
    
    if alias in aliases:
        aliases.remove(alias)
        client.put(key, json.dumps(aliases))
        print(f"Removed alias: {alias}")
    else:
        print(f"Alias not found: {alias}")

def set_email(email):
    """Set email for Let's Encrypt in etcd"""
    client = get_etcd_client()
    key = '/cluster/https/email'
    client.put(key, email)
    print(f"Set email: {email}")

def get_all_domains():
    """Get all domains (primary + aliases) as a list"""
    config = get_https_config()
    domains = []
    
    if 'domain' in config:
        domains.append(config['domain'])
    
    domains.extend(config.get('aliases', []))
    return domains

def delete_https_config():
    """Delete all HTTPS configuration from etcd"""
    client = get_etcd_client()
    
    keys = [
        '/cluster/https/domain',
        '/cluster/https/aliases', 
        '/cluster/https/email'
    ]
    
    deleted_count = 0
    for key in keys:
        if client.delete(key):
            deleted_count += 1
    
    if deleted_count > 0:
        print("HTTPS configuration deleted from etcd")
    else:
        print("No HTTPS configuration found to delete")

def main():
    parser = argparse.ArgumentParser(description='Manage HTTPS domain configuration in etcd')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Set domain command
    domain_parser = subparsers.add_parser('set-domain', help='Set primary domain')
    domain_parser.add_argument('domain', help='Primary domain name')
    
    # Add alias command
    alias_parser = subparsers.add_parser('add-alias', help='Add domain alias')
    alias_parser.add_argument('alias', help='Domain alias to add')
    
    # Remove alias command
    remove_parser = subparsers.add_parser('remove-alias', help='Remove domain alias')
    remove_parser.add_argument('alias', help='Domain alias to remove')
    
    # Set email command
    email_parser = subparsers.add_parser('set-email', help='Set email for Let\'s Encrypt')
    email_parser.add_argument('email', help='Email address')
    
    # Get command
    subparsers.add_parser('get', help='Get current HTTPS configuration')
    
    # List domains command
    subparsers.add_parser('list-domains', help='List all domains (primary + aliases)')
    
    # Delete command
    subparsers.add_parser('delete', help='Delete HTTPS configuration')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'set-domain':
            set_domain(args.domain)
        elif args.command == 'add-alias':
            add_alias(args.alias)
        elif args.command == 'remove-alias':
            remove_alias(args.alias)
        elif args.command == 'set-email':
            set_email(args.email)
        elif args.command == 'get':
            config = get_https_config()
            if config:
                print("HTTPS Configuration:")
                if 'domain' in config:
                    print(f"Primary domain: {config['domain']}")
                if config.get('aliases'):
                    print(f"Aliases: {', '.join(config['aliases'])}")
                if 'email' in config:
                    print(f"Email: {config['email']}")
                
                all_domains = get_all_domains()
                if all_domains:
                    print(f"All domains: {', '.join(all_domains)}")
            else:
                print("No HTTPS configuration found")
        elif args.command == 'list-domains':
            domains = get_all_domains()
            if domains:
                for domain in domains:
                    print(domain)
            else:
                print("No domains configured")
        elif args.command == 'delete':
            delete_https_config()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
