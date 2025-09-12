"""
Rathole tunnel configuration commands
"""

from ..utils import rathole_config

def register_rathole_commands(subparsers):
    """Register rathole management commands"""
    rathole_parser = subparsers.add_parser('rathole', help='Rathole tunnel configuration')
    rathole_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=rathole_parser)
    rathole_subparsers = rathole_parser.add_subparsers(dest='rathole_command', help='Rathole commands')
    
    # Set command
    set_parser = rathole_subparsers.add_parser('set', help='Set rathole configuration')
    set_parser.add_argument('--remote-addr', help='Remote server address (e.g., server.com:2333)')
    set_parser.add_argument('--token', help='Authentication token')
    set_parser.set_defaults(func=rathole_set)
    
    # Get command
    get_parser = rathole_subparsers.add_parser('get', help='Get current rathole configuration')
    get_parser.set_defaults(func=rathole_get)
    
    # Generate client config command
    gen_client_parser = rathole_subparsers.add_parser('generate-client', help='Generate client configuration from etcd')
    gen_client_parser.set_defaults(func=rathole_generate_client)
    
    # Generate SSH-only client config command
    gen_ssh_parser = rathole_subparsers.add_parser('generate-ssh-client', help='Generate SSH-only client configuration from etcd')
    gen_ssh_parser.set_defaults(func=rathole_generate_ssh_client)
    
    # Delete command
    delete_parser = rathole_subparsers.add_parser('delete', help='Delete rathole configuration')
    delete_parser.set_defaults(func=rathole_delete)


def rathole_set(args):
    """Set rathole configuration"""
    rathole_config.set_rathole_config(args.remote_addr, args.token)


def rathole_get(args):
    """Get rathole configuration"""
    rathole_config.get_rathole_config()


def rathole_generate_client(args):
    """Generate client configuration"""
    rathole_config.generate_client_config()


def rathole_generate_ssh_client(args):
    """Generate SSH-only client configuration"""
    rathole_config.generate_ssh_client_config()


def rathole_delete(args):
    """Delete rathole configuration"""
    rathole_config.delete_rathole_config()
