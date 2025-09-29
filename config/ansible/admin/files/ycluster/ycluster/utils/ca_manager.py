#!/usr/bin/env python3
"""
Certificate Authority management for YCluster
"""

import os
import sys
import json
import datetime
import ipaddress
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from ..common.etcd_utils import get_etcd_client

CA_BASE_PATH = "/rbd/misc/ca"
CA_CERT_PATH = f"{CA_BASE_PATH}/ca.crt"
CA_KEY_PATH = f"{CA_BASE_PATH}/ca.key"
CERTS_PATH = f"{CA_BASE_PATH}/certs"

def ensure_ca_directory():
    """Ensure CA directory structure exists"""
    os.makedirs(CA_BASE_PATH, mode=0o700, exist_ok=True)
    os.makedirs(CERTS_PATH, mode=0o755, exist_ok=True)

def is_storage_leader():
    """Check if this node is the storage leader"""
    try:
        client = get_etcd_client()
        leader_key = '/cluster/leader/app'
        leader_value, _ = client.get(leader_key)
        if leader_value:
            leader_hostname = leader_value.decode().strip()
            current_hostname = os.uname().nodename
            return leader_hostname == current_hostname
    except Exception as e:
        print(f"Error checking storage leader status: {e}")
        return False
    return False

def generate_ca():
    """Generate a new Certificate Authority"""
    if not is_storage_leader():
        raise Exception("CA operations can only be performed on the storage leader")
    
    ensure_ca_directory()
    
    # Generate CA private key
    ca_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )
    
    # Create CA certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "YCluster"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Certificate Authority"),
        x509.NameAttribute(NameOID.COMMON_NAME, "YCluster Root CA"),
    ])
    
    ca_cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        ca_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=3650)  # 10 years
    ).add_extension(
        x509.BasicConstraints(ca=True, path_length=None),
        critical=True,
    ).add_extension(
        x509.KeyUsage(
            key_cert_sign=True,
            crl_sign=True,
            digital_signature=False,
            key_encipherment=False,
            key_agreement=False,
            content_commitment=False,
            data_encipherment=False,
            encipher_only=False,
            decipher_only=False
        ),
        critical=True,
    ).sign(ca_key, hashes.SHA256())
    
    # Write CA certificate and key
    with open(CA_CERT_PATH, 'wb') as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    
    with open(CA_KEY_PATH, 'wb') as f:
        f.write(ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    # Set proper permissions
    os.chmod(CA_KEY_PATH, 0o600)
    os.chmod(CA_CERT_PATH, 0o644)
    
    print(f"CA certificate and key generated at {CA_BASE_PATH}")
    return ca_cert, ca_key

def load_ca():
    """Load existing CA certificate and key"""
    if not os.path.exists(CA_CERT_PATH) or not os.path.exists(CA_KEY_PATH):
        raise Exception("CA certificate or key not found. Generate CA first.")
    
    with open(CA_CERT_PATH, 'rb') as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())
    
    with open(CA_KEY_PATH, 'rb') as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    
    return ca_cert, ca_key

def generate_server_cert(hostname, san_list=None):
    """Generate a server certificate signed by the CA"""
    if not is_storage_leader():
        raise Exception("Certificate generation can only be performed on the storage leader")
    
    ensure_ca_directory()
    ca_cert, ca_key = load_ca()
    
    if san_list is None:
        san_list = [hostname]
    
    # Generate server private key
    server_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Create server certificate
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "YCluster"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])
    
    # Build SAN list
    san_names = []
    for san in san_list:
        try:
            # Try to parse as IP address
            ip = ipaddress.ip_address(san)
            san_names.append(x509.IPAddress(ip))
        except ValueError:
            # It's a DNS name
            san_names.append(x509.DNSName(san))
    
    server_cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        ca_cert.subject
    ).public_key(
        server_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName(san_names),
        critical=False,
    ).add_extension(
        x509.KeyUsage(
            key_cert_sign=False,
            crl_sign=False,
            digital_signature=True,
            key_encipherment=True,
            key_agreement=True,
            content_commitment=False,
            data_encipherment=False,
            encipher_only=False,
            decipher_only=False
        ),
        critical=False,
    ).add_extension(
        x509.ExtendedKeyUsage([
            x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
            x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
        ]),
        critical=True,
    ).sign(ca_key, hashes.SHA256())
    
    # Write server certificate and key
    cert_file = f"{CERTS_PATH}/{hostname}.crt"
    key_file = f"{CERTS_PATH}/{hostname}.key"
    
    with open(cert_file, 'wb') as f:
        f.write(server_cert.public_bytes(serialization.Encoding.PEM))
    
    with open(key_file, 'wb') as f:
        f.write(server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    # Set proper permissions
    os.chmod(key_file, 0o600)
    os.chmod(cert_file, 0o644)
    
    print(f"Server certificate generated for {hostname}")
    print(f"Certificate: {cert_file}")
    print(f"Private key: {key_file}")
    
    return server_cert, server_key, cert_file, key_file

def list_certificates():
    """List all generated certificates"""
    if not os.path.exists(CERTS_PATH):
        print("No certificates directory found")
        return
    
    cert_files = list(Path(CERTS_PATH).glob("*.crt"))
    if not cert_files:
        print("No certificates found")
        return
    
    print("Generated certificates:")
    for cert_file in sorted(cert_files):
        try:
            with open(cert_file, 'rb') as f:
                cert = x509.load_pem_x509_certificate(f.read())
            
            hostname = cert_file.stem
            subject_cn = None
            for attr in cert.subject:
                if attr.oid == NameOID.COMMON_NAME:
                    subject_cn = attr.value
                    break
            
            print(f"  {hostname}:")
            print(f"    Subject: {subject_cn}")
            print(f"    Valid from: {cert.not_valid_before}")
            print(f"    Valid until: {cert.not_valid_after}")
            
            # Show SAN
            try:
                san_ext = cert.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                san_list = [str(name) for name in san_ext.value]
                print(f"    SAN: {', '.join(san_list)}")
            except x509.ExtensionNotFound:
                pass
            
        except Exception as e:
            print(f"  {hostname}: Error reading certificate - {e}")

def get_ca_info():
    """Get CA certificate information"""
    try:
        ca_cert, _ = load_ca()
        print("CA Certificate Information:")
        print(f"Subject: {ca_cert.subject}")
        print(f"Valid from: {ca_cert.not_valid_before}")
        print(f"Valid until: {ca_cert.not_valid_after}")
        print(f"Serial number: {ca_cert.serial_number}")
        return True
    except Exception as e:
        print(f"Error loading CA: {e}")
        return False

def revoke_certificate(hostname):
    """Revoke a certificate (remove files)"""
    if not is_storage_leader():
        raise Exception("Certificate revocation can only be performed on the storage leader")
    
    cert_file = f"{CERTS_PATH}/{hostname}.crt"
    key_file = f"{CERTS_PATH}/{hostname}.key"
    
    removed = False
    if os.path.exists(cert_file):
        os.remove(cert_file)
        print(f"Removed certificate: {cert_file}")
        removed = True
    
    if os.path.exists(key_file):
        os.remove(key_file)
        print(f"Removed private key: {key_file}")
        removed = True
    
    if not removed:
        print(f"No certificate found for {hostname}")

def get_ca_cert_path():
    """Get the path to the CA certificate"""
    return CA_CERT_PATH

def get_server_cert_paths(hostname):
    """Get paths to server certificate and key"""
    cert_file = f"{CERTS_PATH}/{hostname}.crt"
    key_file = f"{CERTS_PATH}/{hostname}.key"
    return cert_file, key_file
