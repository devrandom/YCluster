"""
Let's Encrypt certificate management commands
"""

import sys
from pathlib import Path
import subprocess
from ..utils import certbot_manager
from ..common.etcd_utils import get_etcd_client


def register_certbot_commands(subparsers):
    """Register certbot management commands"""
    certbot_parser = subparsers.add_parser('certbot', help='Let\'s Encrypt certificate management')
    certbot_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=certbot_parser)
    certbot_subparsers = certbot_parser.add_subparsers(dest='certbot_command', help='Certbot commands')
    
    # Obtain command
    obtain_parser = certbot_subparsers.add_parser('obtain', help='Obtain a new certificate')
    obtain_parser.add_argument('--test', action='store_true', help='Use staging server (test certificate)')
    obtain_parser.add_argument('-n', '--non-interactive', action='store_true', help='Run in non-interactive mode')
    obtain_parser.set_defaults(func=certbot_obtain)
    
    # Renew command
    renew_parser = certbot_subparsers.add_parser('renew', help='Renew existing certificates')
    renew_parser.add_argument('-n', '--non-interactive', action='store_true', help='Run in non-interactive mode')
    renew_parser.set_defaults(func=certbot_renew)
    
    # List command
    list_parser = certbot_subparsers.add_parser('list', help='List existing certificates')
    list_parser.set_defaults(func=certbot_list)
    
    # Revoke command
    revoke_parser = certbot_subparsers.add_parser('revoke', help='Revoke a certificate')
    revoke_parser.add_argument('domain', help='Domain to revoke certificate for')
    revoke_parser.set_defaults(func=certbot_revoke)
    
    # Delete command
    delete_parser = certbot_subparsers.add_parser('delete', help='Delete a certificate')
    delete_parser.add_argument('domain', help='Domain to delete certificate for')
    delete_parser.set_defaults(func=certbot_delete)
    
    # Update nginx command
    update_parser = certbot_subparsers.add_parser('update-nginx', help='Update nginx configuration with domain from etcd')
    update_parser.set_defaults(func=certbot_update_nginx)
    
    # Status command
    status_parser = certbot_subparsers.add_parser('status', help='Show certificate status and configuration')
    status_parser.set_defaults(func=certbot_status)


def certbot_obtain(args):
    """Obtain certificate"""
    success = certbot_manager.obtain_certificate(test_cert=args.test, non_interactive=args.non_interactive)
    sys.exit(0 if success else 1)


def certbot_renew(args):
    """Renew certificates"""
    success = certbot_manager.renew_certificates(non_interactive=args.non_interactive)
    sys.exit(0 if success else 1)


def certbot_list(args):
    """List certificates"""
    success = certbot_manager.list_certificates()
    sys.exit(0 if success else 1)


def certbot_revoke(args):
    """Revoke certificate"""
    success = certbot_manager.revoke_certificate(args.domain)
    sys.exit(0 if success else 1)


def certbot_delete(args):
    """Delete certificate"""
    success = certbot_manager.delete_certificate(args.domain)
    sys.exit(0 if success else 1)


def certbot_update_nginx(args):
    """Update nginx configuration"""
    success = certbot_manager.update_nginx_configs()
    sys.exit(0 if success else 1)


def certbot_status(args):
    """Show certificate status"""
    config = certbot_manager.get_https_config()
    domains = certbot_manager.get_all_domains(config)
    
    print("HTTPS Configuration:")
    if 'domain' in config:
        print(f"Primary domain: {config['domain']}")
    if config.get('aliases'):
        print(f"Aliases: {', '.join(config['aliases'])}")
    if config.get('email'):
        print(f"Email: {config['email']}")
    else:
        print("Email: Not configured (will use --register-unsafely-without-email)")
    
    # Check if TLS materials exist
    try:
        client = get_etcd_client()
        key_value, _ = client.get('/cluster/tls/key')
        cert_value, _ = client.get('/cluster/tls/cert')
        
        if key_value and cert_value:
            print("TLS materials: Present in etcd")
            
            # Check nginx paths
            nginx_key = Path('/etc/nginx/ssl/key.pem')
            nginx_cert = Path('/etc/nginx/ssl/cert.pem')
            
            if nginx_key.exists() and nginx_cert.exists():
                print("Nginx TLS files: Present")
            else:
                print("Nginx TLS files: Missing (will be created when needed)")
        elif key_value:
            print("TLS private key: Present in etcd, certificate missing")
        elif cert_value:
            print("TLS certificate: Present in etcd, key missing")
        else:
            print("TLS materials: Not found (generate with 'ycluster tls generate')")
    except Exception as e:
        print(f"TLS materials: Error checking ({e})")
    
    if domains:
        print(f"All domains: {', '.join(domains)}")
        
        # Check if certificates exist in etcd
        try:
            client = get_etcd_client()
            cert_value, _ = client.get('/cluster/tls/cert')
            if cert_value:
                print("Let's Encrypt certificate: Present in etcd")
            
                # Try to get certificate expiry
                try:
                    result = subprocess.run(['openssl', 'x509', '-noout', '-enddate'], 
                                          input=cert_value.decode(), text=True, 
                                          capture_output=True)
                    if result.returncode == 0:
                        print(f"Certificate {result.stdout.strip()}")
                except:
                    pass
            else:
                print("Let's Encrypt certificate: Not found")
        except Exception as e:
            print(f"Certificate check error: {e}")
    else:
        print("No domains configured")
