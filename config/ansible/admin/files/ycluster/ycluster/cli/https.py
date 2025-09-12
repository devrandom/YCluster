"""
HTTPS domain configuration commands
"""

from ..utils import https_config

def register_https_commands(subparsers):
    """Register HTTPS management commands"""
    https_parser = subparsers.add_parser('https', help='HTTPS domain configuration')
    https_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=https_parser)
    https_subparsers = https_parser.add_subparsers(dest='https_command', help='HTTPS commands')
    
    # Set domain command
    domain_parser = https_subparsers.add_parser('set-domain', help='Set primary domain')
    domain_parser.add_argument('domain', help='Primary domain name')
    domain_parser.set_defaults(func=https_set_domain)
    
    # Add alias command
    alias_parser = https_subparsers.add_parser('add-alias', help='Add domain alias')
    alias_parser.add_argument('alias', help='Domain alias to add')
    alias_parser.set_defaults(func=https_add_alias)
    
    # Remove alias command
    remove_parser = https_subparsers.add_parser('remove-alias', help='Remove domain alias')
    remove_parser.add_argument('alias', help='Domain alias to remove')
    remove_parser.set_defaults(func=https_remove_alias)
    
    # Set email command
    email_parser = https_subparsers.add_parser('set-email', help='Set email for Let\'s Encrypt')
    email_parser.add_argument('email', help='Email address')
    email_parser.set_defaults(func=https_set_email)
    
    # Get command
    get_parser = https_subparsers.add_parser('get', help='Get current HTTPS configuration')
    get_parser.set_defaults(func=https_get)
    
    # List domains command
    list_parser = https_subparsers.add_parser('list-domains', help='List all domains (primary + aliases)')
    list_parser.set_defaults(func=https_list_domains)
    
    # Delete command
    delete_parser = https_subparsers.add_parser('delete', help='Delete HTTPS configuration')
    delete_parser.set_defaults(func=https_delete)


def https_set_domain(args):
    """Set primary domain"""
    https_config.set_domain(args.domain)


def https_add_alias(args):
    """Add domain alias"""
    https_config.add_alias(args.alias)


def https_remove_alias(args):
    """Remove domain alias"""
    https_config.remove_alias(args.alias)


def https_set_email(args):
    """Set email"""
    https_config.set_email(args.email)


def https_get(args):
    """Get HTTPS configuration"""
    config = https_config.get_https_config()
    if config:
        print("HTTPS Configuration:")
        if 'domain' in config:
            print(f"Primary domain: {config['domain']}")
        if config.get('aliases'):
            print(f"Aliases: {', '.join(config['aliases'])}")
        if 'email' in config:
            print(f"Email: {config['email']}")
        
        all_domains = https_config.get_all_domains()
        if all_domains:
            print(f"All domains: {', '.join(all_domains)}")
    else:
        print("No HTTPS configuration found")


def https_list_domains(args):
    """List all domains"""
    domains = https_config.get_all_domains()
    if domains:
        for domain in domains:
            print(domain)
    else:
        print("No domains configured")


def https_delete(args):
    """Delete HTTPS configuration"""
    https_config.delete_https_config()
