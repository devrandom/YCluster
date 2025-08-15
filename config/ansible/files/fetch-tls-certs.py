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
        with open('/etc/nginx/ssl/cert.pem', 'w') as f:
            f.write(cert_value.decode())
        with open('/etc/nginx/ssl/key.pem', 'w') as f:
            f.write(key_value.decode())
        print('Certificates updated')
    else:
        print('No certificates found in etcd')
        sys.exit(1)

if __name__ == '__main__':
    main()
