"""
DHCP lease and allocation management commands
"""

import sys
from ..utils import lease_manager

def register_dhcp_commands(subparsers):
    """Register DHCP management commands"""
    dhcp_parser = subparsers.add_parser('dhcp', help='DHCP lease and allocation management')
    dhcp_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=dhcp_parser)
    dhcp_subparsers = dhcp_parser.add_subparsers(dest='dhcp_command', help='DHCP commands')
    
    # List commands
    list_parser = dhcp_subparsers.add_parser('list', help='List DHCP leases and allocations')
    list_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=list_parser)
    list_subparsers = list_parser.add_subparsers(dest='list_type', help='What to list')
    
    allocations_parser = list_subparsers.add_parser('allocations', help='List node allocations')
    allocations_parser.set_defaults(func=dhcp_list_allocations)
    
    leases_parser = list_subparsers.add_parser('leases', help='List DHCP leases')
    leases_parser.set_defaults(func=dhcp_list_leases)
    
    all_parser = list_subparsers.add_parser('all', help='List both allocations and leases')
    all_parser.set_defaults(func=dhcp_list_all)
    
    # Delete command
    delete_parser = dhcp_subparsers.add_parser('delete', help='Delete DHCP entries')
    delete_parser.add_argument('target', help='Hostname or MAC address to delete')
    delete_parser.set_defaults(func=dhcp_delete)
    
    # Update hosts command
    update_hosts_parser = dhcp_subparsers.add_parser('update-hosts', help='Update static DNS hosts from etcd')
    update_hosts_parser.set_defaults(func=dhcp_update_hosts)


def dhcp_list_allocations(args):
    """List node allocations"""
    lease_manager.list_allocations()


def dhcp_list_leases(args):
    """List DHCP leases"""
    lease_manager.list_leases()


def dhcp_list_all(args):
    """List both allocations and leases"""
    lease_manager.list_allocations()
    lease_manager.list_leases()


def dhcp_delete(args):
    """Delete DHCP entries"""
    target = args.target
    # Auto-detect if target is hostname or MAC address
    if ':' in target or len(target.replace(':', '').replace('-', '')) == 12:
        # Looks like a MAC address
        success = lease_manager.delete_by_mac(target)
    else:
        # Assume it's a hostname
        success = lease_manager.delete_by_hostname(target)
    
    if success:
        print("Deletion completed successfully")
    else:
        print("Deletion failed")
        sys.exit(1)


def dhcp_update_hosts(args):
    """Update static DNS hosts from etcd"""
    from ..utils import update_dhcp_hosts
    update_dhcp_hosts.main()
