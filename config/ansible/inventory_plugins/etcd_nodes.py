#!/usr/bin/env python3

DOCUMENTATION = '''
    name: etcd_nodes
    plugin_type: inventory
    short_description: Generate inventory from etcd node allocations
    description:
        - Reads node allocations from etcd to generate dynamic inventory
        - Groups hosts by type (storage, compute)
    options:
        plugin:
            description: Name of the plugin
            required: true
            choices: ['etcd_nodes']
        etcd_hosts:
            description: >
                Fallback list of etcd hosts (host:port, no scheme). Normally the
                endpoints come from the cluster's single source of truth instead
                (see below); this is only used when that is unavailable.
            required: false
            default: ['localhost:2379']
        prefix:
            description: etcd key prefix for node data
            required: false
            default: '/cluster/nodes'
        ca_cert:
            description: Fallback path to the etcd CA cert (overridden by ETCD_CACERT)
            required: false
        cert_cert:
            description: Fallback path to the client cert (overridden by ETCD_CERT)
            required: false
        cert_key:
            description: Fallback path to the client key (overridden by ETCD_KEY)
            required: false
    notes:
        - >
            Endpoints and TLS material are resolved from the cluster's single
            source of truth so the controller follows the etcd TLS phase (port +
            certs) automatically with no edits here. Precedence: ETCD_* in the
            live environment, then /etc/ycluster/etcd-client.env (present on core
            controllers), then this plugin's config, then localhost:2379. When
            TLS material is found the client connects over mTLS; otherwise plain.
'''

REQUIREMENTS = ['etcd3']

from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.errors import AnsibleError
import json
import re
import os


class InventoryModule(BaseInventoryPlugin):
    NAME = 'etcd_nodes'

    def verify_file(self, path):
        """Return true/false if this is possibly a valid file for this plugin to consume"""
        valid = False
        if super(InventoryModule, self).verify_file(path):
            if path.endswith(('_etcd.yml', '_etcd.yaml')):
                valid = True
        return valid

    def _resolve_etcd_connection(self, config):
        """Resolve (hosts, tls_kwargs) from the cluster's single source of truth.

        Precedence for each value: live ETCD_* environment, then
        /etc/ycluster/etcd-client.env (present on core controllers), then this
        plugin's config, then a localhost default. This lets the controller
        follow the etcd TLS phase (port + certs) with no edits to inventory_etcd.yml.
        """
        src = {}
        try:
            with open('/etc/ycluster/etcd-client.env') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        src[k] = v
        except OSError:
            pass
        # Live environment wins over the file.
        src.update({k: v for k, v in os.environ.items() if k.startswith('ETCD')})

        if src.get('ETCD_HOSTS'):
            hosts = [h for h in src['ETCD_HOSTS'].split(',') if h]
        else:
            hosts = config.get('etcd_hosts') or ['localhost:2379']

        tls_kwargs = {}
        ca = src.get('ETCD_CACERT') or config.get('ca_cert')
        cert = src.get('ETCD_CERT') or config.get('cert_cert')
        key = src.get('ETCD_KEY') or config.get('cert_key')
        if ca:
            tls_kwargs['ca_cert'] = ca
        if cert and key:
            tls_kwargs['cert_cert'] = cert
            tls_kwargs['cert_key'] = key
        return hosts, tls_kwargs

    def parse(self, inventory, loader, path, cache=True):
        """Parse the inventory file and populate the inventory object"""
        try:
            import etcd3
        except ImportError:
            self.display.warning("etcd3 module is required for etcd_nodes inventory plugin; skipping etcd inventory")
            return
            
        super(InventoryModule, self).parse(inventory, loader, path, cache)

        # Read configuration
        config = self._read_config_data(path)

        prefix = config.get('prefix', '/cluster/nodes')
        etcd_hosts, tls_kwargs = self._resolve_etcd_connection(config)

        # Connect to etcd
        etcd_client = None
        # Disable HTTP proxy for etcd3 client, since they are on this subnet.  grpcio version 1.51 does not
        # support CIDR notation in the `no_proxy` environment variable.
        grpc_options = [('grpc.enable_http_proxy', 0)]

        for host_port in etcd_hosts:
            try:
                host, port = host_port.split(':')
                # tls_kwargs (ca_cert/cert_cert/cert_key) is empty until the
                # cluster is on mTLS; when set, etcd3 connects over TLS.
                etcd_client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options, **tls_kwargs)
                etcd_client.status()  # Test connection
                break
            except:
                continue
        
        if not etcd_client:
            self.display.warning(f"Could not connect to any etcd host: {etcd_hosts}; skipping etcd inventory")
            return

        # Create groups
        self.inventory.add_group('storage')
        self.inventory.add_group('compute')
        self.inventory.add_group('core')
        self.inventory.add_group('etcd')
        self.inventory.add_group('frontend')
        self.inventory.add_group('nas')
        self.inventory.add_group('nvidia')
        self.inventory.add_group('adhoc')
        self.inventory.add_group('all_nodes')
        # Supergroup for generic Ubuntu cluster nodes that get base config
        # (locale, chrony, ycluster package, admin-api, etc.)
        self.inventory.add_group('managed')

        # nvidia is a subgroup of compute
        self.inventory.add_child('compute', 'nvidia')

        # managed = storage + compute (incl. nvidia via compute) + adhoc + nas
        self.inventory.add_child('managed', 'storage')
        self.inventory.add_child('managed', 'compute')
        self.inventory.add_child('managed', 'adhoc')
        self.inventory.add_child('managed', 'nas')

        # Discover actual etcd members from the cluster
        etcd_members = set()
        try:
            members = etcd_client.members
            for member in members:
                # Extract hostname from peer URL (e.g. http://10.0.0.11:2380 -> resolve via inventory later)
                for url in member.peer_urls:
                    host = url.split('://')[1].split(':')[0]
                    etcd_members.add(host)
        except Exception as e:
            self.display.warning(f"Failed to enumerate etcd members: {e}")

        # Read storage leader from etcd
        leader_key = '/cluster/leader/app'
        leader_value, _ = etcd_client.get(leader_key)
        if leader_value:
            leader_hostname = leader_value.decode().strip()
            self.display.vvv(f"Storage leader found: {leader_hostname}")
        else:
            leader_hostname = None
            self.display.vvv("No storage leader found in etcd")

        # Read rathole configuration from etcd if it exists
        try:
            rathole_config_key = f"{prefix}/rathole/config"
            rathole_config_value, _ = etcd_client.get(rathole_config_key)
            if rathole_config_value:
                rathole_config = json.loads(rathole_config_value.decode())
                # Set rathole variables for all hosts
                if 'remote_addr' in rathole_config:
                    self.inventory.set_variable('all', 'rathole_remote_addr', rathole_config['remote_addr'])
                if 'token' in rathole_config:
                    self.inventory.set_variable('all', 'rathole_token', rathole_config['token'])
        except Exception as e:
            self.display.vvv(f"No rathole config found in etcd or failed to parse: {e}")

        # Read allocations from etcd
        for value, metadata in etcd_client.get_prefix(f"{prefix}/by-hostname/"):
            if value:
                try:
                    allocation = json.loads(value.decode())
                    hostname = allocation['hostname']
                    ip_address = allocation['ip']
                    node_type = allocation['type']
                    mac_address = allocation['mac']
                    
                    # Skip AMT hosts (hostnames ending with 'a')
                    if hostname.endswith('a'):
                        continue
                    
                    # Skip DHCP hosts (hostnames starting with 'dhcp-')
                    if hostname.startswith('dhcp-'):
                        continue

                    # Skip disabled nodes — they are allocated in etcd but
                    # not provisioned, so ansible should not target them.
                    if allocation.get('disabled'):
                        continue


                    # Add host to inventory
                    self.inventory.add_host(hostname)
                    
                    # Set host variables
                    self.inventory.set_variable(hostname, 'ansible_host', ip_address)
                    self.inventory.set_variable(hostname, 'mac_address', mac_address)
                    self.inventory.set_variable(hostname, 'node_type', node_type)
                    
                    # Set storage leader flag
                    if hostname == leader_hostname:
                        self.inventory.set_variable(hostname, 'storage_leader', True)
                    else:
                        self.inventory.set_variable(hostname, 'storage_leader', False)
                    
                    # Add to all_nodes group
                    self.inventory.add_child('all_nodes', hostname)
                    
                    # Add to type-specific group
                    if node_type == 'storage':
                        self.inventory.add_child('storage', hostname)
                    elif node_type == 'compute':
                        self.inventory.add_child('compute', hostname)
                    elif node_type == 'macos':
                        if not self.inventory.groups.get('macos'):
                            self.inventory.add_group('macos')
                        self.inventory.add_child('macos', hostname)
                    elif node_type == 'nas':
                        self.inventory.add_child('nas', hostname)
                    elif node_type == 'nvidia':
                        self.inventory.add_child('nvidia', hostname)
                    elif node_type == 'adhoc':
                        self.inventory.add_child('adhoc', hostname)
                    else:
                        if not self.inventory.groups.get('other'):
                            self.inventory.add_group('other')
                        self.inventory.add_child('other', hostname)
                    
                    # Add to core group if matches storage naming (s1+)
                    if re.fullmatch(r's\d+', hostname):
                        self.inventory.add_child('core', hostname)
                    
                    # Add to etcd group if this host is an actual etcd member
                    if ip_address in etcd_members:
                        self.inventory.add_child('etcd', hostname)
                except Exception as e:
                    self.display.warning(f"Failed to parse allocation: {e}")

        # Read frontend nodes from etcd
        frontend_prefix = f"{prefix}/frontend"
        for value, metadata in etcd_client.get_prefix(frontend_prefix):
            if value:
                try:
                    frontend_node = json.loads(value.decode())
                    name = frontend_node.get('name')
                    address = frontend_node.get('ip') or frontend_node.get('hostname')
                    
                    if name and address:
                        # Add host to inventory
                        self.inventory.add_host(name)
                        
                        # Set host variables
                        self.inventory.set_variable(name, 'ansible_host', address)
                        self.inventory.set_variable(name, 'node_type', 'frontend')
                        
                        # Add any additional variables from etcd
                        for key, val in frontend_node.items():
                            if key not in ['name', 'ip', 'hostname']:
                                self.inventory.set_variable(name, key, val)
                        
                        # Set storage leader flag
                        self.inventory.set_variable(name, 'storage_leader', False)
                        
                        # Add to frontend group
                        self.inventory.add_child('frontend', name)
                        
                except Exception as e:
                    self.display.warning(f"Failed to parse frontend node: {e}")
