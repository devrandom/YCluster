#!/usr/bin/env python3
"""
Script to manage Let's Encrypt certificates using certbot with domains from etcd
"""

import json
import sys
import argparse
import etcd3
import os
import subprocess
import tempfile
import re
from pathlib import Path

def get_etcd_client():
    """Get etcd client with connection to available hosts"""
    etcd_hosts = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')
    grpc_options = [('grpc.enable_http_proxy', 0)]
    
    for host_port in etcd_hosts:
        try:
            host, port = host_port.split(':')
            client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
            client.status()  # Test connection
            return client
        except Exception as e:
            print(f"Failed to connect to {host_port}: {e}")
            continue
    
    raise Exception(f"Could not connect to any etcd host: {etcd_hosts}")

def write_tls_key_to_temp():
    """Write TLS private key from etcd to temporary file for CSR generation"""
    client = get_etcd_client()
    
    # Get key from etcd
    key_value, _ = client.get('/cluster/tls/key')
    
    if not key_value:
        print("No TLS private key found in etcd. Generate one with 'tls-config generate' first.")
        return None
    
    # Create temporary file for the key
    temp_key = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
    temp_key.write(key_value.decode())
    temp_key.close()
    
    # Set proper permissions
    os.chmod(temp_key.name, 0o600)
    
    print(f"TLS key written to temporary file: {temp_key.name}")
    
    return temp_key.name

def write_tls_key_to_nginx():
    """Write TLS private key from etcd to nginx path"""
    client = get_etcd_client()
    
    # Get key from etcd
    key_value, _ = client.get('/cluster/tls/key')
    
    if not key_value:
        print("No TLS private key found in etcd. Generate one with 'tls-config generate' first.")
        return None
    
    # Nginx SSL paths (matching the template)
    ssl_dir = Path('/etc/nginx/ssl')
    ssl_dir.mkdir(parents=True, exist_ok=True)
    
    key_path = ssl_dir / 'key.pem'
    
    # Write key only
    key_path.write_text(key_value.decode())
    key_path.chmod(0o600)
    
    print(f"TLS key written to: {key_path}")
    
    return str(key_path)

def generate_csr(key_path, domains):
    """Generate a Certificate Signing Request using our existing private key"""
    primary_domain = domains[0]
    
    # Create temporary CSR file
    temp_csr = tempfile.NamedTemporaryFile(mode='w', suffix='.csr', delete=False)
    temp_csr.close()
    
    # Build openssl command to generate CSR
    cmd = [
        'openssl', 'req', '-new',
        '-key', key_path,
        '-out', temp_csr.name,
        '-subj', f'/CN={primary_domain}',
        '-config', '/dev/stdin'
    ]
    
    # Create config for SAN (Subject Alternative Names)
    config = f"""[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req

[req_distinguished_name]

[v3_req]
subjectAltName = @alt_names

[alt_names]
"""
    
    # Add all domains as SAN entries
    for i, domain in enumerate(domains, 1):
        config += f"DNS.{i} = {domain}\n"
    
    try:
        print(f"Generating CSR for domains: {', '.join(domains)}")
        result = subprocess.run(cmd, input=config, text=True, check=True)
        print(f"CSR generated: {temp_csr.name}")
        return temp_csr.name
    except subprocess.CalledProcessError as e:
        print(f"Failed to generate CSR: {e}")
        # Clean up temp file
        try:
            os.unlink(temp_csr.name)
        except:
            pass
        return None

def update_nginx_config():
    """Update nginx configuration with the primary domain from etcd"""
    config = get_https_config()
    
    if 'domain' not in config:
        print("No primary domain configured - nginx config will use default server_name")
        return False
    
    primary_domain = config['domain']
    nginx_config_path = Path('/etc/nginx/sites-available/admin-api')
    
    # Check if config file exists
    if not nginx_config_path.exists():
        print(f"Nginx config file not found: {nginx_config_path}")
        return False
    
    try:
        # Read current config
        config_content = nginx_config_path.read_text()
        
        # Replace server_name line
        # Look for "server_name _;" or "server_name domain.com;" patterns
        updated_content = re.sub(
            r'(\s+)server_name\s+[^;]+;',
            rf'\1server_name {primary_domain};',
            config_content
        )
        
        # Check if we made any changes
        if updated_content == config_content:
            print(f"Nginx config already has correct server_name: {primary_domain}")
            return True
        
        # Write updated config
        nginx_config_path.write_text(updated_content)
        print(f"Updated nginx server_name to: {primary_domain}")
        
        # Test nginx config
        result = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Nginx config test failed: {result.stderr}")
            return False
        
        # Reload nginx
        result = subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Failed to reload nginx: {result.stderr}")
            return False
        
        print("Nginx configuration updated and reloaded successfully")
        return True
        
    except Exception as e:
        print(f"Error updating nginx config: {e}")
        return False

def ensure_nginx_cert_from_etcd():
    """Ensure nginx has a certificate from etcd (fallback for when no Let's Encrypt cert exists)"""
    client = get_etcd_client()
    
    cert_value, _ = client.get('/cluster/tls/cert')
    if not cert_value:
        return False
    
    cert_path = Path('/etc/nginx/ssl/cert.pem')
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Only write if it doesn't exist (don't overwrite Let's Encrypt certs)
    if not cert_path.exists():
        cert_path.write_text(cert_value.decode())
        cert_path.chmod(0o644)
        print(f"TLS certificate written to: {cert_path}")
    
    return True

def get_https_config():
    """Get HTTPS configuration from etcd"""
    client = get_etcd_client()
    
    domain_key = '/cluster/https/domain'
    aliases_key = '/cluster/https/aliases'
    email_key = '/cluster/https/email'
    
    domain_value, _ = client.get(domain_key)
    aliases_value, _ = client.get(aliases_key)
    email_value, _ = client.get(email_key)
    
    config = {}
    
    if domain_value:
        config['domain'] = domain_value.decode().strip()
    
    if aliases_value:
        try:
            config['aliases'] = json.loads(aliases_value.decode())
        except json.JSONDecodeError:
            config['aliases'] = []
    else:
        config['aliases'] = []
    
    if email_value:
        config['email'] = email_value.decode().strip()
    # No default email - will use --register-unsafely-without-email if not set
    
    return config

def get_all_domains(config):
    """Get all domains (primary + aliases) as a list"""
    domains = []
    
    if 'domain' in config:
        domains.append(config['domain'])
    
    domains.extend(config.get('aliases', []))
    return domains

def run_certbot_command(cmd, check=True):
    """Run certbot command and return result"""
    print(f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, check=check)
        return result
    except subprocess.CalledProcessError as e:
        print(f"Command failed with exit code {e.returncode}")
        raise

def obtain_certificate(test_cert=False, non_interactive=False):
    """Obtain a new certificate from Let's Encrypt using CSR mode"""
    config = get_https_config()
    domains = get_all_domains(config)
    
    if not domains:
        print("No domains configured. Use 'https-config set-domain <domain>' first.")
        return False
    
    if 'domain' not in config:
        print("No primary domain configured. Use 'https-config set-domain <domain>' first.")
        return False
    
    primary_domain = config['domain']
    email = config.get('email')
    
    print(f"Obtaining certificate for domains: {', '.join(domains)}")
    print(f"Primary domain: {primary_domain}")
    if email:
        print(f"Email: {email}")
    else:
        print("No email configured - using --register-unsafely-without-email")
    
    # Write TLS key to temporary file for CSR generation
    temp_key_path = write_tls_key_to_temp()
    if not temp_key_path:
        return False
    
    # Generate CSR with our existing private key
    csr_path = generate_csr(temp_key_path, domains)
    if not csr_path:
        # Clean up temp key file
        try:
            os.unlink(temp_key_path)
        except:
            pass
        return False
    
    # Ensure nginx has a certificate (fallback to etcd cert if no Let's Encrypt cert exists)
    ensure_nginx_cert_from_etcd()
    
    # Create temporary directory for certbot output
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Define specific output file paths
        cert_path = Path(temp_dir) / 'cert.pem'
        chain_path = Path(temp_dir) / 'chain.pem'
        
        # Build certbot command using CSR mode with specific output paths
        cmd = [
            'certbot', 'certonly',
            '--csr', csr_path,
            '--nginx',
            '--agree-tos',
            '--no-eff-email',
            '--cert-path', str(cert_path),
            '--chain-path', str(chain_path)
        ]
        
        # Add non-interactive flag if requested
        if non_interactive:
            cmd.append('--non-interactive')
        
        # Add email or register without email
        if email:
            cmd.extend(['--email', email])
        else:
            cmd.append('--register-unsafely-without-email')
        
        # Add test cert flag if requested
        if test_cert:
            cmd.append('--test-cert')
            print("Using staging server (test certificate)")
        
        run_certbot_command(cmd)
        print("Certificate obtained successfully!")
        
        # Read certificate files from known paths
        if not cert_path.exists():
            print("Error: Certificate file not generated by certbot")
            return False
        
        cert_content = cert_path.read_text()
        
        # Create fullchain (cert + chain)
        if chain_path.exists():
            chain_content = chain_path.read_text()
            fullchain_content = cert_content + chain_content
        else:
            fullchain_content = cert_content
        
        # Store certificate in etcd
        client = get_etcd_client()
        client.put('/cluster/tls/cert', fullchain_content)
        print("Certificate stored in etcd")
        
        # Write certificate to nginx
        write_tls_key_to_nginx()
        nginx_cert_path = Path('/etc/nginx/ssl/cert.pem')
        nginx_cert_path.parent.mkdir(parents=True, exist_ok=True)
        nginx_cert_path.write_text(fullchain_content)
        nginx_cert_path.chmod(0o644)
        print(f"Certificate written to nginx: {nginx_cert_path}")
        
        # Update nginx config with correct domain
        update_nginx_config()
        
        return True
        
    except subprocess.CalledProcessError:
        print("Failed to obtain certificate")
        return False
    finally:
        # Clean up temporary files and directory
        try:
            os.unlink(temp_key_path)
        except:
            pass
        try:
            os.unlink(csr_path)
        except:
            pass
        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

def renew_certificates(non_interactive=False):
    """Renew existing certificates using CSR mode"""
    print("Renewing certificates...")
    
    config = get_https_config()
    if 'domain' not in config:
        print("No primary domain configured for renewal")
        return False
    
    primary_domain = config['domain']
    domains = get_all_domains(config)
    
    # Check if we have a certificate in etcd (our source of truth)
    client = get_etcd_client()
    cert_value, _ = client.get('/cluster/tls/cert')
    if not cert_value:
        print("No existing certificate found in etcd")
        return False
    
    # Write TLS key to temporary file for CSR generation
    temp_key_path = write_tls_key_to_temp()
    if not temp_key_path:
        return False
    
    # Generate CSR with our existing private key
    csr_path = generate_csr(temp_key_path, domains)
    if not csr_path:
        # Clean up temp key file
        try:
            os.unlink(temp_key_path)
        except:
            pass
        return False
    
    # Ensure nginx has a certificate (fallback to etcd cert if no Let's Encrypt cert exists)
    ensure_nginx_cert_from_etcd()
    
    # Create temporary directory for certbot output
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Define specific output file paths
        cert_path = Path(temp_dir) / 'cert.pem'
        chain_path = Path(temp_dir) / 'chain.pem'
        
        # Build certbot renew command using CSR mode with specific output paths
        cmd = [
            'certbot', 'certonly',
            '--csr', csr_path,
            '--nginx',
            '--force-renewal',  # Force renewal even if not due
            '--cert-path', str(cert_path),
            '--chain-path', str(chain_path)
        ]
        
        # Add non-interactive flag if requested
        if non_interactive:
            cmd.append('--non-interactive')
        
        result = run_certbot_command(cmd, check=False)
        if result.returncode == 0:
            print("Certificate renewal completed successfully!")
            
            # Read certificate files from known paths
            if cert_path.exists():
                cert_content = cert_path.read_text()
                
                # Create fullchain (cert + chain)
                if chain_path.exists():
                    chain_content = chain_path.read_text()
                    fullchain_content = cert_content + chain_content
                else:
                    fullchain_content = cert_content
                
                # Store renewed certificate in etcd
                client.put('/cluster/tls/cert', fullchain_content)
                print("Renewed certificate stored in etcd")
                
                # Write certificate to nginx
                write_tls_key_to_nginx()
                nginx_cert_path = Path('/etc/nginx/ssl/cert.pem')
                nginx_cert_path.parent.mkdir(parents=True, exist_ok=True)
                nginx_cert_path.write_text(fullchain_content)
                nginx_cert_path.chmod(0o644)
                print(f"Renewed certificate written to nginx: {nginx_cert_path}")
                
                # Update nginx config with correct domain
                update_nginx_config()
            
            return True
        else:
            print("Certificate renewal completed with warnings")
            return True
            
    except subprocess.CalledProcessError:
        print("Failed to renew certificates")
        return False
    finally:
        # Clean up temporary files and directory
        try:
            os.unlink(temp_key_path)
        except:
            pass
        try:
            os.unlink(csr_path)
        except:
            pass
        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

def store_cert_info(primary_domain, domains):
    """Store certificate information in etcd"""
    try:
        client = get_etcd_client()
        
        cert_info = {
            'primary_domain': primary_domain,
            'domains': domains,
            'cert_path': f'/etc/letsencrypt/live/{primary_domain}/fullchain.pem',
            'key_path': f'/etc/letsencrypt/live/{primary_domain}/privkey.pem',
            'nginx_cert_path': '/etc/nginx/ssl/cert.pem',
            'nginx_key_path': '/etc/nginx/ssl/key.pem',
            'updated_at': subprocess.run(['date', '-Iseconds'], capture_output=True, text=True).stdout.strip()
        }
        
        key = '/cluster/https/certificate_info'
        client.put(key, json.dumps(cert_info))
        print("Certificate information stored in etcd")
    except Exception as e:
        print(f"Warning: Could not store certificate info in etcd: {e}")


def list_certificates():
    """List certificate status from etcd"""
    print("Certificate status:")
    
    try:
        config = get_https_config()
        domains = get_all_domains(config)
        
        if not domains:
            print("No domains configured")
            return True
        
        print(f"Configured domains: {', '.join(domains)}")
        
        # Check etcd for certificate
        client = get_etcd_client()
        cert_value, _ = client.get('/cluster/tls/cert')
        key_value, _ = client.get('/cluster/tls/key')
        
        if cert_value and key_value:
            print("Certificate: Present in etcd")
            
            # Try to get certificate info
            try:
                import subprocess
                result = subprocess.run(['openssl', 'x509', '-noout', '-text'], 
                                      input=cert_value.decode(), text=True, 
                                      capture_output=True)
                if result.returncode == 0:
                    # Extract expiry date
                    for line in result.stdout.split('\n'):
                        if 'Not After' in line:
                            print(f"Expires: {line.strip()}")
                            break
            except:
                pass
        else:
            print("Certificate: Not found in etcd")
        
        return True
    except Exception as e:
        print(f"Error checking certificate status: {e}")
        return False

def revoke_certificate(domain):
    """Revoke a certificate using the certificate from etcd"""
    print(f"Revoking certificate for {domain}...")
    
    try:
        client = get_etcd_client()
        cert_value, _ = client.get('/cluster/tls/cert')
        
        if not cert_value:
            print("No certificate found in etcd")
            return False
        
        # Write certificate to temporary file for revocation
        temp_cert = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
        temp_cert.write(cert_value.decode())
        temp_cert.close()
        
        cmd = ['certbot', 'revoke', '--cert-path', temp_cert.name]
        
        try:
            run_certbot_command(cmd)
            print("Certificate revoked successfully!")
            
            # Remove from etcd
            client.delete('/cluster/tls/cert')
            print("Certificate removed from etcd")
            
            return True
        finally:
            # Clean up temp file
            try:
                os.unlink(temp_cert.name)
            except:
                pass
                
    except subprocess.CalledProcessError:
        print("Failed to revoke certificate")
        return False
    except Exception as e:
        print(f"Error during revocation: {e}")
        return False

def delete_certificate(domain):
    """Delete certificate from etcd and nginx"""
    print(f"Deleting certificate for {domain}...")
    
    try:
        client = get_etcd_client()
        
        # Remove certificate from etcd (keep the key)
        client.delete('/cluster/tls/cert')
        print("Certificate removed from etcd")
        
        # Remove nginx certificate file
        nginx_cert_path = Path('/etc/nginx/ssl/cert.pem')
        if nginx_cert_path.exists():
            nginx_cert_path.unlink()
            print("Certificate removed from nginx")
        
        print("Certificate deleted successfully!")
        return True
    except Exception as e:
        print(f"Error deleting certificate: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description='Manage Let\'s Encrypt certificates with certbot')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Obtain command
    obtain_parser = subparsers.add_parser('obtain', help='Obtain a new certificate')
    obtain_parser.add_argument('--test', action='store_true', help='Use staging server (test certificate)')
    obtain_parser.add_argument('-n', '--non-interactive', action='store_true', help='Run in non-interactive mode')
    
    # Renew command
    renew_parser = subparsers.add_parser('renew', help='Renew existing certificates')
    renew_parser.add_argument('-n', '--non-interactive', action='store_true', help='Run in non-interactive mode')
    
    # List command
    subparsers.add_parser('list', help='List existing certificates')
    
    # Revoke command
    revoke_parser = subparsers.add_parser('revoke', help='Revoke a certificate')
    revoke_parser.add_argument('domain', help='Domain to revoke certificate for')
    
    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete a certificate')
    delete_parser.add_argument('domain', help='Domain to delete certificate for')
    
    # Update nginx command
    subparsers.add_parser('update-nginx', help='Update nginx configuration with domain from etcd')
    
    # Status command
    subparsers.add_parser('status', help='Show certificate status and configuration')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'obtain':
            success = obtain_certificate(test_cert=args.test, non_interactive=args.non_interactive)
            sys.exit(0 if success else 1)
        elif args.command == 'renew':
            success = renew_certificates(non_interactive=args.non_interactive)
            sys.exit(0 if success else 1)
        elif args.command == 'list':
            success = list_certificates()
            sys.exit(0 if success else 1)
        elif args.command == 'revoke':
            success = revoke_certificate(args.domain)
            sys.exit(0 if success else 1)
        elif args.command == 'delete':
            success = delete_certificate(args.domain)
            sys.exit(0 if success else 1)
        elif args.command == 'update-nginx':
            success = update_nginx_config()
            sys.exit(0 if success else 1)
        elif args.command == 'status':
            config = get_https_config()
            domains = get_all_domains(config)
            
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
                    print("TLS materials: Not found (generate with 'tls-config generate')")
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
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
