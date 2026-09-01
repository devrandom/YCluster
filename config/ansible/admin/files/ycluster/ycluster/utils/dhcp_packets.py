"""Packet construction helpers for the DHCP server."""

import netifaces
from scapy.all import get_if_hwaddr
from scapy.layers.dhcp import BOOTP, DHCP
from scapy.layers.inet import IP, UDP
from scapy.layers.l2 import Ether

LEASE_TIME = 43200  # 12 hours
GATEWAY = '10.0.0.254'
DNS_SERVER = '10.0.0.254'
NTP_SERVER = '10.0.0.254'


def interface_for_ip(ip):
    """Get the network interface bound to an IP address."""
    for interface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(interface)
        if netifaces.AF_INET in addrs:
            for addr in addrs[netifaces.AF_INET]:
                if addr['addr'] == ip:
                    return interface
    raise ValueError(f"No interface found for IP {ip}")


def build_dhcp_options(server_ip, message_type, hostname=''):
    """Build common DHCP options for OFFER and ACK packets."""
    options = [
        ('message-type', message_type),
        ('server_id', server_ip),
        ('lease_time', LEASE_TIME),
        ('subnet_mask', '255.255.255.0'),
        ('router', GATEWAY),
        ('name_server', DNS_SERVER),
        ('NTP_server', NTP_SERVER),
        ('tftp_server_name', server_ip),  # Option 66
        ('boot-file-name', 'EFI/BOOT/grubx64.efi')  # Option 67
    ]

    if hostname:
        options.insert(-1, ('hostname', hostname))

    options.append('end')
    return options


def _reply(server_ip, request_packet, message_type, options,
           assigned_ip=None):
    interface = interface_for_ip(server_ip)
    fields = {
        'op': 2,  # Boot reply
        'htype': 1,
        'hlen': 6,
        'xid': request_packet[BOOTP].xid,
        'chaddr': request_packet[BOOTP].chaddr,
    }
    if assigned_ip is not None:
        fields['yiaddr'] = assigned_ip
        fields['siaddr'] = server_ip
    return (
        Ether(dst=request_packet[Ether].src, src=get_if_hwaddr(interface)) /
        IP(src=server_ip, dst='255.255.255.255') /
        UDP(sport=67, dport=68) /
        BOOTP(**fields) /
        DHCP(options=options)
    )


def create_dhcp_offer(server_ip, request_packet, offered_ip, hostname=''):
    """Create a DHCP OFFER packet."""
    options = build_dhcp_options(server_ip, 2, hostname)
    return _reply(server_ip, request_packet, 2, options, offered_ip)


def create_dhcp_ack(server_ip, request_packet, ack_ip, hostname=''):
    """Create a DHCP ACK packet."""
    options = build_dhcp_options(server_ip, 5, hostname)
    return _reply(server_ip, request_packet, 5, options, ack_ip)


def create_dhcp_nak(server_ip, request_packet):
    """Create a DHCP NAK packet."""
    options = [
        ('message-type', 6),
        ('server_id', server_ip),
        'end'
    ]
    return _reply(server_ip, request_packet, 6, options)
