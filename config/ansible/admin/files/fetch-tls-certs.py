#!/usr/bin/env python3
"""
Script to fetch TLS certificates from etcd and write them to nginx SSL directory
"""

import etcd3
import os
import sys

def main():
    etcd_hosts = os.environ.get('ETCD_HOSTS', 'localhost:2379').split(',')
    grpc_options = [('grpc.enable_http_proxy', 0)]
    
    for host_port in etcd_hosts:
        try:
            host, port = host_port.split(':')
            client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
            client.status()
            break
        except:
            continue
    else:
        print('Could not connect to etcd')
        sys.exit(1)
    
    cert_value, _ = client.get('/cluster/tls/cert')
    key_value, _ = client.get('/cluster/tls/key')
    
    if cert_value and key_value:
        cert_content = cert_value.decode()
        key_content = key_value.decode()
        
        cert_path = '/etc/nginx/ssl/cert.pem'
        key_path = '/etc/nginx/ssl/key.pem'
        
        # Check if files exist and compare content
        cert_changed = True
        key_changed = True
        
        try:
            with open(cert_path, 'r') as f:
                existing_cert = f.read()
            cert_changed = existing_cert != cert_content
        except FileNotFoundError:
            pass  # File doesn't exist, so it's a change
        
        try:
            with open(key_path, 'r') as f:
                existing_key = f.read()
            key_changed = existing_key != key_content
        except FileNotFoundError:
            pass  # File doesn't exist, so it's a change
        
        # Only write if content has changed
        if cert_changed:
            with open(cert_path, 'w') as f:
                f.write(cert_content)
        
        if key_changed:
            with open(key_path, 'w') as f:
                f.write(key_content)
        
        if cert_changed or key_changed:
            print('Certificates updated')
        else:
            print('Certificates unchanged')
    else:
        print('No certificates found in etcd')
        sys.exit(1)

if __name__ == '__main__':
    main()
