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
            description: List of etcd hosts
            required: false
            default: ['localhost:2379']
        prefix:
            description: etcd key prefix for node data
            required: false
            default: '/cluster/nodes'
'''

REQUIREMENTS = ['etcd3']

from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.errors import AnsibleError
import json
import re


class InventoryModule(BaseInventoryPlugin):
    NAME = 'etcd_nodes'

    def verify_file(self, path):
        """Return true/false if this is possibly a valid file for this plugin to consume"""
        valid = False
        if super(InventoryModule, self).verify_file(path):
            if path.endswith(('_etcd.yml', '_etcd.yaml')):
                valid = True
        return valid

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
        
        etcd_hosts = config.get('etcd_hosts', ['localhost:2379'])
        prefix = config.get('prefix', '/cluster/nodes')
        
        # Connect to etcd
        etcd_client = None
        # Disable HTTP proxy for etcd3 client, since they are on this subnet.  grpcio version 1.51 does not
        # support CIDR notation in the `no_proxy` environment variable.
        grpc_options = [('grpc.enable_http_proxy', 0)]

        for host_port in etcd_hosts:
            try:
                host, port = host_port.split(':')
                etcd_client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
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
        self.inventory.add_group('frontend')
        self.inventory.add_group('all_nodes')

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
                    else:
                        if not self.inventory.groups.get('other'):
                            self.inventory.add_group('other')
                        self.inventory.add_child('other', hostname)
                    
                    # Add to core group if matches core naming (s1-s3)
                    if re.fullmatch(r's[1-3]', hostname):
                        self.inventory.add_child('core', hostname)
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
