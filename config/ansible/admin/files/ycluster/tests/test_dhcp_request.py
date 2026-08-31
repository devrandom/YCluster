"""Tests for DHCP request parsing and reply dispatch."""

import unittest
from types import SimpleNamespace
from unittest import mock

from ycluster.utils import dhcp_packets
from ycluster.utils import dhcp_server


class FakePacket:
    def __init__(self, options, ciaddr=None):
        self.layers = {
            dhcp_server.Ether: SimpleNamespace(src='mac-a'),
            dhcp_server.DHCP: SimpleNamespace(options=options),
        }
        if ciaddr is not None:
            self.layers[dhcp_server.BOOTP] = SimpleNamespace(
                ciaddr=ciaddr, xid=0x123, chaddr=b'mac-a')

    def haslayer(self, layer):
        return layer in self.layers

    def __getitem__(self, layer):
        return self.layers[layer]


class DHCPRequestTests(unittest.TestCase):
    def make_server(self, leases=None):
        server = dhcp_server.DHCPServer.__new__(dhcp_server.DHCPServer)
        server.leases = leases or {}
        server.allocated_ips = {'lease-address'}
        server.server_ip = 'server-address'
        return server

    def test_request_details_reads_options_and_ciaddr(self):
        packet = FakePacket(
            [('requested_addr', 'offer-address'),
             ('hostname', b'host-a'),
             ('server_id', 'server-address')],
            ciaddr='renew-address')
        packet.layers[dhcp_server.DHCP].options.append('end')
        requested_ip, hostname, server_id = self.make_server()._request_details(packet)
        self.assertEqual((requested_ip, hostname, server_id),
                         ('offer-address', 'host-a', 'server-address'))

        packet = FakePacket([], ciaddr='renew-address')
        requested_ip, hostname, server_id = self.make_server()._request_details(packet)
        self.assertEqual((requested_ip, hostname, server_id),
                         ('renew-address', None, None))

    def test_reallocate_check(self):
        server = self.make_server(
            {'mac-a': {'hostname': 'old', 'ip': 'lease-address'}})
        self.assertFalse(server._request_should_reallocate('mac-a', None))
        self.assertFalse(
            server._request_should_reallocate('mac-a', 'ubuntu-server'))
        self.assertTrue(
            server._request_should_reallocate('mac-a', 'new'))
        with mock.patch.object(server, 'determine_ip_from_hostname',
                               return_value='hostname-address'):
            self.assertTrue(
                server._request_should_reallocate('mac-a', 'old'))

    def test_ack_and_nak_dispatch(self):
        packet = FakePacket([('requested_addr', 'lease-address')])
        server = self.make_server(
            {'mac-a': {'hostname': 'host-a', 'ip': 'lease-address'}})
        with mock.patch.object(dhcp_server, 'create_dhcp_ack') as ack, \
                mock.patch.object(dhcp_server, 'interface_for_ip',
                                  return_value='iface'), \
                mock.patch.object(dhcp_server, 'sendp') as send:
            server.handle_dhcp_request(packet)
        ack.assert_called_once_with(
            'server-address', packet, 'lease-address', 'host-a')
        self.assertEqual(send.call_count, 1)

        packet = FakePacket([])
        with mock.patch.object(dhcp_server, 'create_dhcp_nak') as nak, \
                mock.patch.object(dhcp_server, 'interface_for_ip',
                                  return_value='iface'), \
                mock.patch.object(dhcp_server, 'sendp') as send:
            server.handle_dhcp_request(packet)
        nak.assert_called_once_with('server-address', packet)
        self.assertEqual(send.call_count, 1)


class DHCPPacketBuilderTests(unittest.TestCase):
    def request(self):
        return FakePacket([], ciaddr='renew-address')

    def test_interface_for_ip(self):
        addresses = {'iface': {dhcp_packets.netifaces.AF_INET: [
            {'addr': 'server-address'}]}}
        with mock.patch.object(dhcp_packets.netifaces, 'interfaces',
                               return_value=addresses.keys()), \
                mock.patch.object(dhcp_packets.netifaces, 'ifaddresses',
                                  side_effect=addresses.get):
            self.assertEqual(
                dhcp_packets.interface_for_ip('server-address'), 'iface')

    def test_build_options_includes_hostname_before_end(self):
        options = dhcp_packets.build_dhcp_options('server-address', 5, 'host-a')
        self.assertEqual(options[-3], ('hostname', 'host-a'))
        self.assertEqual(options[-1], 'end')

    def test_create_replies(self):
        server_ip = dhcp_packets.GATEWAY
        with mock.patch.object(dhcp_packets, 'interface_for_ip',
                               return_value='iface'), \
                mock.patch.object(dhcp_packets, 'get_if_hwaddr',
                                  return_value='server-mac'):
            offer = dhcp_packets.create_dhcp_offer(
                server_ip, self.request(), server_ip, 'host-a')
            ack = dhcp_packets.create_dhcp_ack(
                server_ip, self.request(), server_ip, 'host-b')
            nak = dhcp_packets.create_dhcp_nak(server_ip, self.request())

        self.assertEqual(offer[dhcp_packets.BOOTP].yiaddr, server_ip)
        self.assertEqual(ack[dhcp_packets.BOOTP].yiaddr, server_ip)
        self.assertEqual(nak[dhcp_packets.BOOTP].yiaddr, '0.0.0.0')
        self.assertEqual(offer[dhcp_packets.DHCP].options[-1], 'end')


if __name__ == '__main__':
    unittest.main()
