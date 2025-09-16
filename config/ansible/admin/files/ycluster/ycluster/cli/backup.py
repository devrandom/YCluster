"""Backup encryption management commands."""

import json
import sys
from ..common.etcd_utils import get_etcd_client


def register_backup_commands(subparsers):
    """Register backup-related commands."""
    backup_parser = subparsers.add_parser('backup', help='Backup encryption management')
    backup_parser.set_defaults(func=handle_backup_command)
    backup_subparsers = backup_parser.add_subparsers(dest='backup_command', help='Backup commands')
    
    # Recipients management
    recipients_parser = backup_subparsers.add_parser('recipients', help='Manage backup encryption recipients')
    recipients_subparsers = recipients_parser.add_subparsers(dest='recipients_command', help='Recipients commands')
    
    # List recipients
    list_parser = recipients_subparsers.add_parser('list', help='List backup encryption recipients')
    list_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    list_parser.set_defaults(func=backup_recipients_list)
    
    # Add recipient
    add_parser = recipients_subparsers.add_parser('add', help='Add backup encryption recipient')
    add_parser.add_argument('name', help='Recipient name/identifier')
    add_parser.add_argument('public_key', help='Age public key (age1...)')
    add_parser.add_argument('--description', help='Optional description')
    add_parser.set_defaults(func=backup_recipients_add)
    
    # Remove recipient
    remove_parser = recipients_subparsers.add_parser('remove', help='Remove backup encryption recipient')
    remove_parser.add_argument('name', help='Recipient name to remove')
    remove_parser.set_defaults(func=backup_recipients_remove)
    
    # Show recipient
    show_parser = recipients_subparsers.add_parser('show', help='Show recipient details')
    show_parser.add_argument('name', help='Recipient name to show')
    show_parser.set_defaults(func=backup_recipients_show)
    
    # Destinations management
    destinations_parser = backup_subparsers.add_parser('destinations', help='Manage backup rsync destinations')
    destinations_subparsers = destinations_parser.add_subparsers(dest='destinations_command', help='Destinations commands')
    
    # List destinations
    dest_list_parser = destinations_subparsers.add_parser('list', help='List backup rsync destinations')
    dest_list_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    dest_list_parser.set_defaults(func=backup_destinations_list)
    
    # Add destination
    dest_add_parser = destinations_subparsers.add_parser('add', help='Add backup rsync destination')
    dest_add_parser.add_argument('name', help='Destination name/identifier')
    dest_add_parser.add_argument('url', help='Rsync destination URL (user@host:/path or rsync://host/module)')
    dest_add_parser.add_argument('--enabled', action='store_true', default=True, help='Enable destination (default: true)')
    dest_add_parser.add_argument('--disabled', action='store_true', help='Disable destination')
    dest_add_parser.set_defaults(func=backup_destinations_add)
    
    # Remove destination
    dest_remove_parser = destinations_subparsers.add_parser('remove', help='Remove backup rsync destination')
    dest_remove_parser.add_argument('name', help='Destination name to remove')
    dest_remove_parser.set_defaults(func=backup_destinations_remove)
    
    # Show destination
    dest_show_parser = destinations_subparsers.add_parser('show', help='Show destination details')
    dest_show_parser.add_argument('name', help='Destination name to show')
    dest_show_parser.set_defaults(func=backup_destinations_show)
    
    # Enable/disable destination
    dest_enable_parser = destinations_subparsers.add_parser('enable', help='Enable backup destination')
    dest_enable_parser.add_argument('name', help='Destination name to enable')
    dest_enable_parser.set_defaults(func=backup_destinations_enable)
    
    dest_disable_parser = destinations_subparsers.add_parser('disable', help='Disable backup destination')
    dest_disable_parser.add_argument('name', help='Destination name to disable')
    dest_disable_parser.set_defaults(func=backup_destinations_disable)


def backup_recipients_list(args):
    """List all backup encryption recipients."""
    try:
        client = get_etcd_client()
        
        # Get all recipients from etcd
        result = client.get_prefix('/cluster/backup/recipients/')
        
        recipients = []
        
        for value, metadata in result:
            try:
                recipient_data = json.loads(value.decode('utf-8'))
                name = metadata.key.decode('utf-8').split('/')[-1]
                
                recipient_info = {
                    'name': name,
                    'public_key': recipient_data.get('public_key', 'N/A'),
                    'created_at': recipient_data.get('created_at', 'N/A')
                }
                
                if recipient_data.get('description'):
                    recipient_info['description'] = recipient_data['description']
                
                recipients.append(recipient_info)
                
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"Error parsing recipient data: {e}")
                continue
        
        if args.json:
            print(json.dumps(recipients, indent=2))
        else:
            if not recipients:
                print("No backup encryption recipients configured.")
                return
            
            print("Backup Encryption Recipients:")
            print("-" * 50)
            
            for recipient in recipients:
                print(f"Name: {recipient['name']}")
                print(f"  Public Key: {recipient['public_key']}")
                if 'description' in recipient:
                    print(f"  Description: {recipient['description']}")
                print(f"  Added: {recipient['created_at']}")
                print()
                
    except Exception as e:
        print(f"Error listing recipients: {e}")
        sys.exit(1)


def backup_recipients_add(args):
    """Add a backup encryption recipient."""
    try:
        # Validate public key format
        if not args.public_key.startswith('age1'):
            print("Error: Public key must start with 'age1'")
            sys.exit(1)
        
        client = get_etcd_client()
        key = f'/cluster/backup/recipients/{args.name}'
        
        # Check if recipient already exists
        existing = client.get(key)
        if existing[0] is not None:
            print(f"Error: Recipient '{args.name}' already exists")
            sys.exit(1)
        
        # Create recipient data
        from datetime import datetime
        recipient_data = {
            'public_key': args.public_key,
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }
        
        if args.description:
            recipient_data['description'] = args.description
        
        # Store in etcd
        client.put(key, json.dumps(recipient_data))
        
        print(f"Added backup encryption recipient: {args.name}")
        print(f"Public key: {args.public_key}")
        if args.description:
            print(f"Description: {args.description}")
            
    except Exception as e:
        print(f"Error adding recipient: {e}")
        sys.exit(1)


def backup_recipients_remove(args):
    """Remove a backup encryption recipient."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/recipients/{args.name}'
        
        # Check if recipient exists
        existing = client.get(key)
        if existing[0] is None:
            print(f"Error: Recipient '{args.name}' not found")
            sys.exit(1)
        
        # Remove from etcd
        client.delete(key)
        
        print(f"Removed backup encryption recipient: {args.name}")
        
    except Exception as e:
        print(f"Error removing recipient: {e}")
        sys.exit(1)


def backup_recipients_show(args):
    """Show details of a specific recipient."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/recipients/{args.name}'
        
        result = client.get(key)
        if result[0] is None:
            print(f"Error: Recipient '{args.name}' not found")
            sys.exit(1)
        
        try:
            recipient_data = json.loads(result[0].decode('utf-8'))
            
            print(f"Recipient: {args.name}")
            print("-" * 30)
            print(f"Public Key: {recipient_data.get('public_key', 'N/A')}")
            if recipient_data.get('description'):
                print(f"Description: {recipient_data['description']}")
            print(f"Added: {recipient_data.get('created_at', 'N/A')}")
            
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error parsing recipient data: {e}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error showing recipient: {e}")
        sys.exit(1)


def backup_destinations_list(args):
    """List all backup rsync destinations."""
    try:
        client = get_etcd_client()
        
        # Get all destinations from etcd
        result = client.get_prefix('/cluster/backup/destinations/')
        
        destinations = []
        
        for value, metadata in result:
            try:
                destination_data = json.loads(value.decode('utf-8'))
                name = metadata.key.decode('utf-8').split('/')[-1]
                
                destination_info = {
                    'name': name,
                    'url': destination_data.get('url', 'N/A'),
                    'enabled': destination_data.get('enabled', True),
                    'created_at': destination_data.get('created_at', 'N/A')
                }
                
                destinations.append(destination_info)
                
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"Error parsing destination data: {e}")
                continue
        
        if args.json:
            print(json.dumps(destinations, indent=2))
        else:
            if not destinations:
                print("No backup rsync destinations configured.")
                return
            
            print("Backup Rsync Destinations:")
            print("-" * 50)
            
            for destination in destinations:
                status = "ENABLED" if destination['enabled'] else "DISABLED"
                print(f"Name: {destination['name']} ({status})")
                print(f"  URL: {destination['url']}")
                print(f"  Added: {destination['created_at']}")
                print()
                
    except Exception as e:
        print(f"Error listing destinations: {e}")
        sys.exit(1)


def backup_destinations_add(args):
    """Add a backup rsync destination."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/destinations/{args.name}'
        
        # Check if destination already exists
        existing = client.get(key)
        if existing[0] is not None:
            print(f"Error: Destination '{args.name}' already exists")
            sys.exit(1)
        
        # Determine enabled status
        enabled = True
        if args.disabled:
            enabled = False
        elif hasattr(args, 'enabled'):
            enabled = args.enabled
        
        # Create destination data
        from datetime import datetime
        destination_data = {
            'url': args.url,
            'enabled': enabled,
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }
        
        # Store in etcd
        client.put(key, json.dumps(destination_data))
        
        status = "enabled" if enabled else "disabled"
        print(f"Added backup rsync destination: {args.name} ({status})")
        print(f"URL: {args.url}")
            
    except Exception as e:
        print(f"Error adding destination: {e}")
        sys.exit(1)


def backup_destinations_remove(args):
    """Remove a backup rsync destination."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/destinations/{args.name}'
        
        # Check if destination exists
        existing = client.get(key)
        if existing[0] is None:
            print(f"Error: Destination '{args.name}' not found")
            sys.exit(1)
        
        # Remove from etcd
        client.delete(key)
        
        print(f"Removed backup rsync destination: {args.name}")
        
    except Exception as e:
        print(f"Error removing destination: {e}")
        sys.exit(1)


def backup_destinations_show(args):
    """Show details of a specific destination."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/destinations/{args.name}'
        
        result = client.get(key)
        if result[0] is None:
            print(f"Error: Destination '{args.name}' not found")
            sys.exit(1)
        
        try:
            destination_data = json.loads(result[0].decode('utf-8'))
            
            status = "ENABLED" if destination_data.get('enabled', True) else "DISABLED"
            print(f"Destination: {args.name} ({status})")
            print("-" * 30)
            print(f"URL: {destination_data.get('url', 'N/A')}")
            print(f"Added: {destination_data.get('created_at', 'N/A')}")
            
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error parsing destination data: {e}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error showing destination: {e}")
        sys.exit(1)


def backup_destinations_enable(args):
    """Enable a backup rsync destination."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/destinations/{args.name}'
        
        # Check if destination exists
        result = client.get(key)
        if result[0] is None:
            print(f"Error: Destination '{args.name}' not found")
            sys.exit(1)
        
        try:
            destination_data = json.loads(result[0].decode('utf-8'))
            destination_data['enabled'] = True
            
            # Update in etcd
            client.put(key, json.dumps(destination_data))
            
            print(f"Enabled backup rsync destination: {args.name}")
            
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error parsing destination data: {e}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error enabling destination: {e}")
        sys.exit(1)


def backup_destinations_disable(args):
    """Disable a backup rsync destination."""
    try:
        client = get_etcd_client()
        key = f'/cluster/backup/destinations/{args.name}'
        
        # Check if destination exists
        result = client.get(key)
        if result[0] is None:
            print(f"Error: Destination '{args.name}' not found")
            sys.exit(1)
        
        try:
            destination_data = json.loads(result[0].decode('utf-8'))
            destination_data['enabled'] = False
            
            # Update in etcd
            client.put(key, json.dumps(destination_data))
            
            print(f"Disabled backup rsync destination: {args.name}")
            
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error parsing destination data: {e}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error disabling destination: {e}")
        sys.exit(1)


def handle_backup_command(args):
    """Handle backup command dispatch."""
    if not hasattr(args, 'backup_command') or not args.backup_command:
        print("Error: No backup command specified")
        print("Available commands: recipients, destinations")
        sys.exit(1)
    
    # This should not be reached since subparsers handle the dispatch
    print(f"Error: Unknown backup command: {args.backup_command}")
    sys.exit(1)
