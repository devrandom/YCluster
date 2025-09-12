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
