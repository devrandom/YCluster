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

    def _connect(self, etcd3, hosts, tls_kwargs):
        """Try each configured etcd host, returning the first usable client."""
        grpc_options = [('grpc.enable_http_proxy', 0)]
        for host_port in hosts:
            try:
                host, port = host_port.split(':')
                client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options, **tls_kwargs)
                client.status()
                return client
            except Exception as e:
                self.display.warning(f"etcd inventory: {host_port} unreachable: {e}")
        return None

    def _create_groups(self):
        """Create the static group structure and parent/child links."""
        for group in ('storage', 'compute', 'core', 'etcd', 'frontend', 'nas',
                      'nvidia', 'adhoc', 'all_nodes', 'managed'):
            self.inventory.add_group(group)
        self.inventory.add_child('compute', 'nvidia')
        for child in ('storage', 'compute', 'adhoc', 'nas'):
            self.inventory.add_child('managed', child)

    def _etcd_members(self, client):
        """Return the set of live etcd member IPs, or an empty set on failure."""
        members = set()
        try:
            for member in client.members:
                for url in member.peer_urls:
                    members.add(url.split('://')[1].split(':')[0])
        except Exception as e:
            self.display.warning(f"Failed to enumerate etcd members: {e}")
        return members

    def _warn_membership_drift(self, etcd_members):
        """Warn when live etcd membership differs from the static group."""
        static = self.inventory.groups.get('etcd')
        if not etcd_members or not static:
            return
        static_ips = {h.vars.get('ansible_host') for h in static.get_hosts()}
        static_ips.discard(None)
        if static_ips and static_ips != etcd_members:
            self.display.warning(
                f"etcd inventory: live etcd membership {sorted(etcd_members)} "
                f"differs from the static etcd group {sorted(static_ips)} — "
                f"update the static quorum definition (or investigate the cluster)")

    def _storage_leader(self, client):
        """Read the storage leader hostname from etcd, or None."""
        value, _ = client.get('/cluster/leader/app')
        if value:
            leader = value.decode().strip()
            self.display.vvv(f"Storage leader found: {leader}")
            return leader
        self.display.vvv("No storage leader found in etcd")
        return None

    def _add_allocation(self, inventory, value, leader_hostname, etcd_members):
        """Add one node allocation to the inventory."""
        try:
            allocation = json.loads(value.decode())
            hostname = allocation['hostname']
            ip_address = allocation['ip']
            node_type = allocation['type']
            mac_address = allocation['mac']

            # Skip AMT aliases and unprovisioned DHCP/disabled nodes.
            if hostname.endswith('a') or hostname.startswith('dhcp-') \
                    or allocation.get('disabled'):
                return
        except Exception as e:
            self.display.warning(f"Failed to parse allocation: {e}")
            return

        self._add_allocation_host(hostname, ip_address, node_type, mac_address,
                                  leader_hostname, etcd_members)

    def _add_allocation_host(self, hostname, ip_address, node_type,
                             mac_address, leader_hostname, etcd_members):
        self.inventory.add_host(hostname)
        self.inventory.set_variable(hostname, 'ansible_host', ip_address)
        self.inventory.set_variable(hostname, 'mac_address', mac_address)
        self.inventory.set_variable(hostname, 'node_type', node_type)
        self.inventory.set_variable(hostname, 'storage_leader',
                                    hostname == leader_hostname)
        self.inventory.add_child('all_nodes', hostname)
        self._add_type_group(node_type, hostname)
        if re.fullmatch(r's\d+', hostname):
            self.inventory.add_child('core', hostname)
        if ip_address in etcd_members:
            self.inventory.add_child('etcd', hostname)

    def _add_type_group(self, node_type, hostname):
        """Add a node to its type-specific group, creating it when needed."""
        if node_type == 'macos' and not self.inventory.groups.get('macos'):
            self.inventory.add_group('macos')
        groups = {'storage': 'storage', 'compute': 'compute', 'macos': 'macos',
                  'nas': 'nas', 'nvidia': 'nvidia', 'adhoc': 'adhoc'}
        group = groups.get(node_type, 'other')
        if group == 'other' and not self.inventory.groups.get('other'):
            self.inventory.add_group('other')
        self.inventory.add_child(group, hostname)

    def _add_frontends(self, client, prefix):
        """Add frontend nodes from etcd."""
        for value, metadata in client.get_prefix(prefix):
            if value:
                self._add_frontend(value)

    def _add_frontend(self, value):
        try:
            node = json.loads(value.decode())
            name = node.get('name')
            address = node.get('ip') or node.get('hostname')
            if not name or not address:
                return
            self.inventory.add_host(name)
            self.inventory.set_variable(name, 'ansible_host', address)
            self.inventory.set_variable(name, 'node_type', 'frontend')
            for key, val in node.items():
                if key not in ('name', 'ip', 'hostname'):
                    self.inventory.set_variable(name, key, val)
            self.inventory.set_variable(name, 'storage_leader', False)
            self.inventory.add_child('frontend', name)
        except Exception as e:
            self.display.warning(f"Failed to parse frontend node: {e}")

    def parse(self, inventory, loader, path, cache=True):
        """Parse the inventory file and populate the inventory object"""
        try:
            import etcd3
        except ImportError:
            self.display.warning("etcd3 module is required for etcd_nodes inventory plugin; skipping etcd inventory")
            return

        super(InventoryModule, self).parse(inventory, loader, path, cache)
        config = self._read_config_data(path)
        prefix = config.get('prefix', '/cluster/nodes')
        etcd_hosts, tls_kwargs = self._resolve_etcd_connection(config)

        etcd_client = self._connect(etcd3, etcd_hosts, tls_kwargs)
        if not etcd_client:
            self.display.warning(f"Could not connect to any etcd host: {etcd_hosts}; skipping etcd inventory")
            return

        self._create_groups()
        etcd_members = self._etcd_members(etcd_client)
        self._warn_membership_drift(etcd_members)
        leader_hostname = self._storage_leader(etcd_client)

        for value, metadata in etcd_client.get_prefix(f"{prefix}/by-hostname/"):
            if value:
                self._add_allocation(inventory, value, leader_hostname, etcd_members)

        self._add_frontends(etcd_client, f"{prefix}/frontend")
