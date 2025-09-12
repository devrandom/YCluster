"""
Storage and RBD management commands
"""

import sys
from ..storage import user_rbd

def register_storage_commands(subparsers):
    """Register storage management commands"""
    storage_parser = subparsers.add_parser('storage', help='Storage and RBD management')
    storage_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=storage_parser)
    storage_subparsers = storage_parser.add_subparsers(dest='storage_command', help='Storage commands')
    
    # User RBD commands
    rbd_parser = storage_subparsers.add_parser('rbd', help='User RBD volume management')
    rbd_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=rbd_parser)
    rbd_subparsers = rbd_parser.add_subparsers(dest='rbd_command', help='RBD commands')
    
    # RBD start command
    start_parser = rbd_subparsers.add_parser('start', help='Acquire RBD lock and mount volume')
    start_parser.add_argument('-K', action='store_true', help='Use LUKS passphrase instead of Clevis')
    start_parser.set_defaults(func=storage_rbd_start)
    
    # RBD stop command
    stop_parser = rbd_subparsers.add_parser('stop', help='Unmount volume and release RBD lock')
    stop_parser.set_defaults(func=storage_rbd_stop)
    
    # RBD status command
    status_parser = rbd_subparsers.add_parser('status', help='Show current volume status')
    status_parser.set_defaults(func=storage_rbd_status)
    
    # RBD check command
    check_parser = rbd_subparsers.add_parser('check', help='Test if volume can be decrypted')
    check_parser.add_argument('-K', action='store_true', help='Use LUKS passphrase instead of Clevis')
    check_parser.set_defaults(func=storage_rbd_check)
    
    # RBD bind command
    bind_parser = rbd_subparsers.add_parser('bind', help='Ensure Clevis binding is correct')
    bind_parser.add_argument('-k', '--key-file', required=True, help='Passphrase file')
    bind_parser.set_defaults(func=storage_rbd_bind)


def storage_rbd_start(args):
    """Start user RBD"""
    success = user_rbd.rbd_start(use_passphrase=args.K)
    if success:
        print("User RBD successfully acquired and mounted")
        sys.exit(0)
    else:
        print("Failed to acquire User RBD lock")
        sys.exit(1)


def storage_rbd_stop(args):
    """Stop user RBD"""
    user_rbd.rbd_stop()


def storage_rbd_status(args):
    """Show RBD status"""
    user_rbd.rbd_status()


def storage_rbd_check(args):
    """Check RBD decrypt capability"""
    success = user_rbd.rbd_check(use_passphrase=args.K)
    sys.exit(0 if success else 1)


def storage_rbd_bind(args):
    """Bind RBD to Clevis"""
    result = user_rbd.rbd_bind(args.key_file)
    if result == 0:
        print("Clevis binding already correct")
        sys.exit(0)
    elif result == 1:
        print("Clevis binding updated successfully")
        sys.exit(1)
    else:
        print("Clevis binding failed")
        sys.exit(2)
