#!/usr/bin/env python3

DOCUMENTATION = '''
    name: dnsmasq_leases
    plugin_type: inventory
    short_description: Generate inventory from dnsmasq leases file
    description:
        - Parses dnsmasq leases file to generate dynamic inventory
        - Groups hosts by hostname prefix (s=storage, c=compute)
    options:
        plugin:
            description: Name of the plugin
            required: true
            choices: ['dnsmasq_leases']
        leases_file:
            description: Path to dnsmasq leases file
            required: false
            default: '/data/dnsmasq.leases'
        compose:
            description: Create vars from jinja2 expressions
            type: dict
            default: {}
        groups:
            description: Add hosts to group based on Jinja2 conditionals
            type: dict
            default: {}
        keyed_groups:
            description: Add hosts to group based on the values of a variable
            type: list
            default: []
'''

EXAMPLES = '''
# Example inventory configuration
plugin: dnsmasq_leases
leases_file: /data/dnsmasq.leases
'''

from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.errors import AnsibleError
import os
import time


class InventoryModule(BaseInventoryPlugin):
    NAME = 'dnsmasq_leases'

    def verify_file(self, path):
        """Return true/false if this is possibly a valid file for this plugin to consume"""
        valid = False
        if super(InventoryModule, self).verify_file(path):
            # Check if the config file mentions this plugin
            if path.endswith(('dnsmasq_leases.yml', 'dnsmasq_leases.yaml')):
                valid = True
        return valid

    def parse(self, inventory, loader, path, cache=True):
        """Parse the inventory file and populate the inventory object"""
        super(InventoryModule, self).parse(inventory, loader, path, cache)

        # Read configuration from the inventory file
        config = self._read_config_data(path)
        
        # Get the leases file path
        leases_file = config.get('leases_file', '/data/dnsmasq.leases')
        
        # Check if leases file exists
        if not os.path.exists(leases_file):
            raise AnsibleError(f"Leases file not found: {leases_file}")

        # Create groups
        self.inventory.add_group('storage')
        self.inventory.add_group('compute')
        self.inventory.add_group('all_nodes')

        # Parse leases file
        self._parse_leases_file(leases_file)

    def _parse_leases_file(self, leases_file):
        """Parse the dnsmasq leases file and add hosts to inventory"""
        try:
            with open(leases_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Parse lease line format: timestamp mac_address ip_address hostname client_id
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    
                    timestamp, mac_address, ip_address, hostname = parts[:4]
                    
                    # Note: timestamp of 0 means infinite lease in dnsmasq
                    
                    # Skip if hostname is '*' (no hostname)
                    if hostname == '*':
                        continue
                    
                    # Add host to inventory
                    self.inventory.add_host(hostname)
                    
                    # Set host variables
                    self.inventory.set_variable(hostname, 'ansible_host', ip_address)
                    self.inventory.set_variable(hostname, 'mac_address', mac_address)
                    self.inventory.set_variable(hostname, 'lease_timestamp', timestamp)
                    
                    # Add to all_nodes group
                    self.inventory.add_child('all_nodes', hostname)
                    
                    # Determine group based on hostname prefix
                    if hostname.startswith('s'):
                        self.inventory.add_child('storage', hostname)
                        self.inventory.set_variable(hostname, 'node_type', 'storage')
                    elif hostname.startswith('c'):
                        self.inventory.add_child('compute', hostname)
                        self.inventory.set_variable(hostname, 'node_type', 'compute')
                    else:
                        # For hosts that don't match s/c pattern, add to a generic group
                        if not self.inventory.get_group('other'):
                            self.inventory.add_group('other')
                        self.inventory.add_child('other', hostname)
                        self.inventory.set_variable(hostname, 'node_type', 'other')
                        
        except IOError as e:
            raise AnsibleError(f"Could not read leases file {leases_file}: {e}")
