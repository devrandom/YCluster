#!/usr/bin/env python3
"""
Script to manage TLS certificates in etcd
"""

import json
import sys
import argparse
import datetime
from typing import cast
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ..common.etcd_utils import get_etcd_client

# types-cryptography's Encoding/PrivateFormat stubs declare members as `str`;
# cast through to the real Enum class so we can pass them where the stubs
# require Encoding/PrivateFormat.
_PEM: serialization.Encoding = cast(serialization.Encoding, serialization.Encoding.PEM)
_PKCS8: serialization.PrivateFormat = cast(serialization.PrivateFormat, serialization.PrivateFormat.PKCS8)

def generate_self_signed_cert(common_name="cluster.local", san_list=None):
    """Generate a self-signed certificate"""
    if san_list is None:
        san_list = ["10.0.0.254", "cluster.local", "localhost"]
    
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Create certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cluster"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    
    # Build SAN list
    san_names: list[x509.GeneralName] = []
    for san in san_list:
        try:
            # Try to parse as IP address
            import ipaddress
            ip = ipaddress.ip_address(san)
            san_names.append(x509.IPAddress(ip))
        except ValueError:
            # It's a DNS name
            san_names.append(x509.DNSName(san))

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc)
    ).not_valid_after(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName(san_names),
        critical=False,
    ).sign(private_key, hashes.SHA256())
    
    # Serialize certificate and key
    cert_pem = cert.public_bytes(_PEM)
    key_pem = private_key.private_bytes(
        encoding=_PEM,
        format=_PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    return cert_pem.decode(), key_pem.decode()

def set_tls_cert(cert_pem, key_pem):
    """Set TLS certificate and key in etcd"""
    client = get_etcd_client()
    
    cert_key = '/cluster/tls/cert'
    key_key = '/cluster/tls/key'
    
    client.put(cert_key, cert_pem)
    client.put(key_key, key_pem)
    print("TLS certificate and key stored in etcd")

def get_tls_cert():
    """Get TLS certificate from etcd"""
    client = get_etcd_client()
    
    cert_key = '/cluster/tls/cert'
    key_key = '/cluster/tls/key'
    
    cert_value, _ = client.get(cert_key)
    key_value, _ = client.get(key_key)
    
    if cert_value and key_value:
        print("Certificate found in etcd")
        print("Certificate:")
        print(cert_value.decode())
        print("\nPrivate key: [REDACTED]")
        
        # Parse and show certificate info
        try:
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(cert_value)
            print(f"\nCertificate Info:")
            print(f"Subject: {cert.subject}")
            print(f"Issuer: {cert.issuer}")
            print(f"Valid from: {cert.not_valid_before}")
            print(f"Valid until: {cert.not_valid_after}")
            
            # Show SAN
            try:
                san_ext = cert.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                print(f"Subject Alternative Names: {list(san_ext.value)}")
            except x509.ExtensionNotFound:
                print("No Subject Alternative Names")
                
        except Exception as e:
            print(f"Could not parse certificate: {e}")
        return True
    else:
        print("No TLS certificate found in etcd")
        return False

def delete_tls_cert():
    """Delete TLS certificate from etcd"""
    client = get_etcd_client()
    
    cert_key = '/cluster/tls/cert'
    key_key = '/cluster/tls/key'
    
    cert_deleted = client.delete(cert_key)
    key_deleted = client.delete(key_key)
    
    if cert_deleted or key_deleted:
        print("TLS certificate and key deleted from etcd")
    else:
        print("No TLS certificate found to delete")

def get_common_name_from_etcd():
    """Get common name from etcd if it exists"""
    try:
        client = get_etcd_client()
        key = '/cluster/tls/common_name'
        value, _ = client.get(key)
        if value:
            return value.decode().strip()
    except Exception as e:
        print(f"Could not get common name from etcd: {e}")
    return None

def set_common_name_in_etcd(common_name):
    """Set common name in etcd"""
    client = get_etcd_client()
    key = '/cluster/tls/common_name'
    client.put(key, common_name)
    print(f"Set common name in etcd: {common_name}")

def generate_and_store_cert(common_name, san_list):
    """Generate a new certificate and store it in etcd"""
    # Get common name from etcd if not provided and exists
    if not common_name:
        common_name = get_common_name_from_etcd()
    
    # Use default if still not set
    if not common_name:
        common_name = "cluster.local"
    
    print(f"Generating self-signed certificate for {common_name}")
    if san_list:
        print(f"Subject Alternative Names: {', '.join(san_list)}")
    
    cert_pem, key_pem = generate_self_signed_cert(common_name, san_list)
    set_tls_cert(cert_pem, key_pem)
    
    # Store the common name in etcd for future use
    set_common_name_in_etcd(common_name)

def _cmd_generate(args):
    san_list = args.san or ["10.0.0.254", "cluster.local", "localhost"]
    generate_and_store_cert(args.common_name, san_list)


def _cmd_set_common_name(args):
    set_common_name_in_etcd(args.common_name)


def _cmd_get_common_name(args):
    common_name = get_common_name_from_etcd()
    if common_name:
        print(f"Common name: {common_name}")
    else:
        print("No common name set in etcd")


def _cmd_set(args):
    with open(args.cert_file, 'r') as f:
        cert_pem = f.read()
    with open(args.key_file, 'r') as f:
        key_pem = f.read()
    set_tls_cert(cert_pem, key_pem)


def _cmd_get(args):
    if not get_tls_cert():
        sys.exit(1)


def _build_parser():
    parser = argparse.ArgumentParser(description='Manage TLS certificates in etcd')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    gen_parser = subparsers.add_parser('generate', help='Generate and store a new self-signed certificate')
    gen_parser.add_argument('--common-name', help='Common name for the certificate (defaults to value from etcd or cluster.local)')
    gen_parser.add_argument('--san', action='append', help='Subject Alternative Name (can be used multiple times)')

    cn_parser = subparsers.add_parser('set-common-name', help='Set common name in etcd')
    cn_parser.add_argument('common_name', help='Common name to store in etcd')

    subparsers.add_parser('get-common-name', help='Get common name from etcd')

    set_parser = subparsers.add_parser('set', help='Set certificate and key from files')
    set_parser.add_argument('--cert-file', required=True, help='Path to certificate file')
    set_parser.add_argument('--key-file', required=True, help='Path to private key file')

    subparsers.add_parser('get', help='Get current TLS certificate')
    subparsers.add_parser('delete', help='Delete TLS certificate')
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        'generate': lambda: _cmd_generate(args),
        'set-common-name': lambda: _cmd_set_common_name(args),
        'get-common-name': lambda: _cmd_get_common_name(args),
        'set': lambda: _cmd_set(args),
        'get': lambda: _cmd_get(args),
        'delete': delete_tls_cert,
    }
    try:
        commands[args.command]()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
