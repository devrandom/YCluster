"""
frp tunnel configuration commands
"""

from ..utils import frp_config

def register_frp_commands(subparsers):
    """Register frp management commands"""
    frp_parser = subparsers.add_parser('frp', help='frp tunnel configuration')
    frp_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=frp_parser)
    frp_subparsers = frp_parser.add_subparsers(dest='frp_command', help='frp commands')

    # Set command
    set_parser = frp_subparsers.add_parser('set', help='Set frp configuration')
    set_parser.add_argument('--remote-addr', help='Remote server address (e.g., server.com:7000)')
    set_parser.add_argument('--token', help='Authentication token')
    set_parser.set_defaults(func=frp_set)

    # Get command
    get_parser = frp_subparsers.add_parser('get', help='Get current frp configuration')
    get_parser.set_defaults(func=frp_get)

    # Generate client config command
    gen_client_parser = frp_subparsers.add_parser('generate-client', help='Generate client configuration from etcd')
    gen_client_parser.set_defaults(func=frp_generate_client)

    # Generate SSH-only client config command
    gen_ssh_parser = frp_subparsers.add_parser('generate-ssh-client', help='Generate SSH-only client configuration from etcd')
    gen_ssh_parser.set_defaults(func=frp_generate_ssh_client)

    # Delete command
    delete_parser = frp_subparsers.add_parser('delete', help='Delete frp configuration')
    delete_parser.set_defaults(func=frp_delete)


def frp_set(args):
    """Set frp configuration"""
    frp_config.set_frp_config(args.remote_addr, args.token)


def frp_get(args):
    """Get frp configuration"""
    frp_config.get_frp_config()


def frp_generate_client(args):
    """Generate client configuration"""
    frp_config.generate_client_config()


def frp_generate_ssh_client(args):
    """Generate SSH-only client configuration"""
    frp_config.generate_ssh_client_config()


def frp_delete(args):
    """Delete frp configuration"""
    frp_config.delete_frp_config()
