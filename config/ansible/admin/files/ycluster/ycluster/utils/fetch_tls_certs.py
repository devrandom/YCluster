#!/usr/bin/env python3
"""
Script to fetch TLS certificates from etcd and write them to nginx SSL directory
"""

import sys

from ..common.etcd_utils import get_etcd_client

def _read_if_changed(path, content):
    try:
        with open(path, 'r') as f:
            return f.read() != content
    except FileNotFoundError:
        return True


def _write_updated_files(cert_content, key_content):
    cert_path = '/etc/nginx/ssl/cert.pem'
    key_path = '/etc/nginx/ssl/key.pem'

    cert_changed = _read_if_changed(cert_path, cert_content)
    key_changed = _read_if_changed(key_path, key_content)

    if cert_changed:
        with open(cert_path, 'w') as f:
            f.write(cert_content)
    if key_changed:
        with open(key_path, 'w') as f:
            f.write(key_content)

    return cert_changed or key_changed


def main():
    try:
        client = get_etcd_client()
    except Exception as e:
        print(f'Could not connect to etcd: {e}')
        sys.exit(1)
    
    cert_value, _ = client.get('/cluster/tls/cert')
    key_value, _ = client.get('/cluster/tls/key')
    
    if cert_value and key_value:
        changed = _write_updated_files(cert_value.decode(), key_value.decode())
        print('Certificates updated' if changed else 'Certificates unchanged')
    else:
        print('No certificates found in etcd')
        sys.exit(1)

if __name__ == '__main__':
    main()
