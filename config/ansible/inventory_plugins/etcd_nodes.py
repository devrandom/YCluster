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

from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.errors import AnsibleError
import json
import etcd3


class InventoryModule(BaseInventoryPlugin):
    NAME = 'etcd_nodes'

    def verify_file(self, path):
        """Return true/false if this is possibly a valid file for this plugin to consume"""
        valid = False
        if super(InventoryModule, self).verify_file(path):
            if path.endswith(('etcd_nodes.yml', 'etcd_nodes.yaml')):
                valid = True
        return valid

    def parse(self, inventory, loader, path, cache=True):
        """Parse the inventory file and populate the inventory object"""
        super(InventoryModule, self).parse(inventory, loader, path, cache)

        # Read configuration
        config = self._read_config_data(path)
        
        etcd_hosts = config.get('etcd_hosts', ['localhost:2379'])
        prefix = config.get('prefix', '/cluster/nodes')
        
        # Connect to etcd
        etcd_client = None
        for host_port in etcd_hosts:
            try:
                host, port = host_port.split(':')
                etcd_client = etcd3.client(host=host, port=int(port))
                etcd_client.status()  # Test connection
                break
            except:
                continue
        
        if not etcd_client:
            raise AnsibleError(f"Could not connect to any etcd host: {etcd_hosts}")

        # Create groups
        self.inventory.add_group('storage')
        self.inventory.add_group('compute')
        self.inventory.add_group('all_nodes')

        # Read allocations from etcd
        for value, metadata in etcd_client.get_prefix(f"{prefix}/by-hostname/"):
            if value:
                try:
                    allocation = json.loads(value.decode())
                    hostname = allocation['hostname']
                    ip_address = allocation['ip']
                    node_type = allocation['type']
                    mac_address = allocation['mac']
                    
                    # Add host to inventory
                    self.inventory.add_host(hostname)
                    
                    # Set host variables
                    self.inventory.set_variable(hostname, 'ansible_host', ip_address)
                    self.inventory.set_variable(hostname, 'mac_address', mac_address)
                    self.inventory.set_variable(hostname, 'node_type', node_type)
                    
                    # Add to all_nodes group
                    self.inventory.add_child('all_nodes', hostname)
                    
                    # Add to type-specific group
                    if node_type == 'storage':
                        self.inventory.add_child('storage', hostname)
                    elif node_type == 'compute':
                        self.inventory.add_child('compute', hostname)
                    else:
                        if not self.inventory.get_group('other'):
                            self.inventory.add_group('other')
                        self.inventory.add_child('other', hostname)
                        
                except Exception as e:
                    self.display.warning(f"Failed to parse allocation: {e}")
