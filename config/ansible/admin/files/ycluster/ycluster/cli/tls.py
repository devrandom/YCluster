"""
TLS certificate management commands
"""

import sys
from ..utils import tls_config

def register_tls_commands(subparsers):
    """Register TLS management commands"""
    tls_parser = subparsers.add_parser('tls', help='TLS certificate management')
    tls_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=tls_parser)
    tls_subparsers = tls_parser.add_subparsers(dest='tls_command', help='TLS commands')
    
    # Generate command
    gen_parser = tls_subparsers.add_parser('generate', help='Generate and store a new self-signed certificate')
    gen_parser.add_argument('--common-name', help='Common name for the certificate')
    gen_parser.add_argument('--san', action='append', help='Subject Alternative Name (can be used multiple times)')
    gen_parser.set_defaults(func=tls_generate)
    
    # Set common name command
    cn_parser = tls_subparsers.add_parser('set-common-name', help='Set common name in etcd')
    cn_parser.add_argument('common_name', help='Common name to store in etcd')
    cn_parser.set_defaults(func=tls_set_common_name)
    
    # Get common name command
    get_cn_parser = tls_subparsers.add_parser('get-common-name', help='Get common name from etcd')
    get_cn_parser.set_defaults(func=tls_get_common_name)
    
    # Set command
    set_parser = tls_subparsers.add_parser('set', help='Set certificate and key from files')
    set_parser.add_argument('--cert-file', required=True, help='Path to certificate file')
    set_parser.add_argument('--key-file', required=True, help='Path to private key file')
    set_parser.set_defaults(func=tls_set)
    
    # Get command
    get_parser = tls_subparsers.add_parser('get', help='Get current TLS certificate')
    get_parser.set_defaults(func=tls_get)
    
    # Delete command
    delete_parser = tls_subparsers.add_parser('delete', help='Delete TLS certificate')
    delete_parser.set_defaults(func=tls_delete)
    
    # Fetch certs command
    fetch_parser = tls_subparsers.add_parser('fetch-certs', help='Fetch TLS certificates from etcd to nginx')
    fetch_parser.set_defaults(func=tls_fetch_certs)
    
    # CA management commands
    ca_parser = tls_subparsers.add_parser('ca', help='Certificate Authority management')
    ca_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=ca_parser)
    ca_subparsers = ca_parser.add_subparsers(dest='ca_command', help='CA commands')
    
    # CA generate command
    ca_gen_parser = ca_subparsers.add_parser('generate', help='Generate a new Certificate Authority')
    ca_gen_parser.set_defaults(func=ca_generate)
    
    # CA info command
    ca_info_parser = ca_subparsers.add_parser('info', help='Show CA certificate information')
    ca_info_parser.set_defaults(func=ca_info)
    
    # CA generate server cert command
    ca_server_parser = ca_subparsers.add_parser('generate-server', help='Generate a server certificate signed by CA')
    ca_server_parser.add_argument('hostname', help='Hostname for the certificate')
    ca_server_parser.add_argument('--san', action='append', help='Subject Alternative Name (can be used multiple times)')
    ca_server_parser.set_defaults(func=ca_generate_server)
    
    # CA list certificates command
    ca_list_parser = ca_subparsers.add_parser('list', help='List all generated certificates')
    ca_list_parser.set_defaults(func=ca_list)
    
    # CA revoke certificate command
    ca_revoke_parser = ca_subparsers.add_parser('revoke', help='Revoke a certificate')
    ca_revoke_parser.add_argument('hostname', help='Hostname of certificate to revoke')
    ca_revoke_parser.set_defaults(func=ca_revoke)


def tls_generate(args):
    """Generate TLS certificate"""
    san_list = args.san or ["10.0.0.254", "cluster.local", "localhost"]
    tls_config.generate_and_store_cert(args.common_name, san_list)


def tls_set_common_name(args):
    """Set common name"""
    tls_config.set_common_name_in_etcd(args.common_name)


def tls_get_common_name(args):
    """Get common name"""
    common_name = tls_config.get_common_name_from_etcd()
    if common_name:
        print(f"Common name: {common_name}")
    else:
        print("No common name set in etcd")


def tls_set(args):
    """Set TLS certificate from files"""
    with open(args.cert_file, 'r') as f:
        cert_pem = f.read()
    with open(args.key_file, 'r') as f:
        key_pem = f.read()
    tls_config.set_tls_cert(cert_pem, key_pem)


def tls_get(args):
    """Get TLS certificate"""
    if not tls_config.get_tls_cert():
        sys.exit(1)


def tls_delete(args):
    """Delete TLS certificate"""
    tls_config.delete_tls_cert()


def tls_fetch_certs(args):
    """Fetch TLS certificates from etcd to nginx"""
    from ..utils import fetch_tls_certs
    fetch_tls_certs.main()


def ca_generate(args):
    """Generate CA certificate"""
    from ..utils import ca_manager
    try:
        ca_manager.generate_ca()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def ca_info(args):
    """Show CA information"""
    from ..utils import ca_manager
    if not ca_manager.get_ca_info():
        sys.exit(1)


def ca_generate_server(args):
    """Generate server certificate"""
    from ..utils import ca_manager
    try:
        ca_manager.generate_server_cert(args.hostname, args.san)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def ca_list(args):
    """List certificates"""
    from ..utils import ca_manager
    ca_manager.list_certificates()


def ca_revoke(args):
    """Revoke certificate"""
    from ..utils import ca_manager
    try:
        ca_manager.revoke_certificate(args.hostname)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
