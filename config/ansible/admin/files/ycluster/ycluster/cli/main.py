#!/usr/bin/env python3
"""
Main CLI entry point for YCluster management tools
"""

import sys
import argparse
from .. import __version__


def create_parser():
    """Create the main argument parser"""
    parser = argparse.ArgumentParser(
        prog='ycluster',
        description='YCluster Infrastructure Management Tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        '--version', 
        action='version', 
        version=f'ycluster {__version__}'
    )
    
    subparsers = parser.add_subparsers(
        dest='command',
        help='Available commands',
        metavar='COMMAND'
    )
    
    # Import and register subcommands
    from .cluster import register_cluster_commands
    from .dhcp import register_dhcp_commands
    from .tls import register_tls_commands
    from .https import register_https_commands
    from .certbot import register_certbot_commands
    from .rathole import register_rathole_commands
    from .frontend import register_frontend_commands

    register_cluster_commands(subparsers)
    register_dhcp_commands(subparsers)
    register_tls_commands(subparsers)
    register_https_commands(subparsers)
    register_certbot_commands(subparsers)
    register_rathole_commands(subparsers)
    register_frontend_commands(subparsers)

    return parser


def main():
    """Main CLI entry point"""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Execute the command
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
