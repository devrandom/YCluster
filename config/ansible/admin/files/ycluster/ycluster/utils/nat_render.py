"""Standalone nftables renderer for the frontend nat-reconciler.

Deliberately dependency-free (stdlib only) so it can be copied to a
frontend node outside the cluster envelope without pulling in the
ycluster package (which needs etcd3 + argon2). The canonical source of
this file is
config/ansible/admin/files/ycluster/ycluster/utils/nat_render.py;
install-frontend-nat.yml copies it to /usr/local/lib/ycluster/.
"""

import hashlib
import ipaddress
import re

TRANSIT_SUPERNET = '100.100.0.0/16'
PROTOS = ('tcp', 'udp')

_PORT_RE = re.compile(r'^([0-9]{1,5})(?:-([0-9]{1,5}))?$')


def _parse_ports(spec):
    """Return (first, last) for '80' or '20000-20099'."""
    m = _PORT_RE.match(spec.strip())
    if not m:
        raise ValueError(f'invalid port spec: {spec!r}')
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if not (1 <= first <= 65535 and 1 <= last <= 65535 and first <= last):
        raise ValueError(f'invalid port spec: {spec!r}')
    return first, last


def transit_subnet(value):
    """Normalize an IP or /24 CIDR to its transit /24 network string."""
    try:
        if '/' in value:
            network = ipaddress.ip_network(value, strict=False)
        else:
            addr = ipaddress.ip_address(value)
            network = ipaddress.ip_network(f'{addr}/24', strict=False)
    except ValueError as exc:
        raise ValueError(f'invalid transit subnet or IP: {value!r}') from exc
    if (network.prefixlen != 24 or
            network.network_address not in
            ipaddress.ip_network(TRANSIT_SUPERNET)):
        raise ValueError(
            f'transit subnet must be a /24 in {TRANSIT_SUPERNET}: '
            f'{value!r}')
    return str(network)


def _transit_subnets(exposures):
    """Return the deterministic set of allocated transit /24s."""
    return sorted({
        transit_subnet(e['transit_ip']) for e in exposures
        if e.get('transit_ip')
    })


def validate_exposures(exposures):
    """Raise ValueError on invalid proto, IP, or overlapping ranges."""
    by_proto = {}
    for e in exposures:
        if e['proto'] not in PROTOS:
            raise ValueError(f'invalid proto: {e["proto"]!r}')
        first, last = _parse_ports(e['ports'])
        if e.get('transit_ip'):
            transit_subnet(e['transit_ip'])
        by_proto.setdefault(e['proto'], []).append((first, last, e))
    for proto, ranges in by_proto.items():
        ranges.sort()
        for (f1, l1, _), (f2, l2, _) in zip(ranges, ranges[1:]):
            if f2 <= l1:
                raise ValueError(
                    f'overlapping {proto} port ranges: '
                    f'{f1}-{l1} and {f2}-{l2}')
    return True


def digest_of(exposures, public_ip, wan_iface='eth0',
              transit_iface='wg-vm-nat'):
    """Return the 12-char digest embedded in the rendered ruleset.

    The agent computes the same digest from the fetched exposure list
    and compares it against the live table's comment, so a rebooted
    frontend (base table, no comment) always converges.
    """
    body = _render_body(exposures, public_ip, wan_iface, transit_iface)
    return hashlib.sha256('\n'.join(body).encode()).hexdigest()[:12]


def render_nftables(exposures, public_ip, wan_iface='eth0',
                    transit_iface='wg-vm-nat'):
    """Render a complete, deterministic nftables ruleset.

    Fail-closed: the table's forward chain policy is drop for every
    protocol. Egress is explicit per-protocol (TCP/UDP new flows,
    ICMP established/related only). Anti-spoofing via iifname/oifname
    + source address. The rendered text embeds its own digest as a
    table comment so the agent can compare live vs applied.
    """
    validate_exposures(exposures)
    digest = digest_of(exposures, public_ip, wan_iface, transit_iface)
    lines = [
        # Atomic replace idiom: see docs/design/vm-frontend-nat.md.
        'table ip ycluster_vm_nat',
        'delete table ip ycluster_vm_nat',
        'table ip ycluster_vm_nat {',
        f'  comment "digest {digest}"',
    ]
    lines += _render_body(exposures, public_ip, wan_iface, transit_iface)
    lines.append('}')
    return '\n'.join(lines) + '\n'


def _render_body(exposures, public_ip, wan_iface, transit_iface):
    subnets = _transit_subnets(exposures)
    lines = [
        '  chain prerouting {',
        '    type nat hook prerouting priority dstnat; policy accept;',
    ]
    for e in exposures:
        first, last = _parse_ports(e['ports'])
        if not e.get('transit_ip'):
            continue  # quarantined until the reconciler allocates an IP
        dport = f'{first}' if first == last else f'{first}-{last}'
        lines.append(
            f'    iifname "{wan_iface}" ip daddr {public_ip} '
            f'{e["proto"]} dport {dport} '
            f'dnat to {e["transit_ip"]}')
    lines += [
        '  }',
        '  chain postrouting {',
        '    type nat hook postrouting priority srcnat; policy accept;',
    ]
    for subnet in subnets:
        lines.append(
            f'    ip saddr {subnet} oifname "{wan_iface}" '
            f'snat ip to {public_ip}')
    lines += [
        '  }',
        '  chain forward {',
        '    type filter hook forward priority filter; policy drop;',
    ]
    # Private/cluster drops must precede established/related. Otherwise a
    # pre-existing transit -> private flow survives a ruleset apply.
    for subnet in subnets:
        lines += [
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 10.0.0.0/8 drop',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 172.16.0.0/12 drop',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 192.168.0.0/16 drop',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 169.254.0.0/16 drop',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 100.64.0.0/10 drop',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip daddr 100.100.0.0/16 drop',
        ]
    lines += [
        '    ct state established,related accept',
    ]
    for e in exposures:
        first, last = _parse_ports(e['ports'])
        if not e.get('transit_ip'):
            continue
        dport = f'{first}' if first == last else f'{first}-{last}'
        lines.append(
            f'    iifname "{wan_iface}" oifname "{transit_iface}" '
            f'ip daddr {e["transit_ip"]} {e["proto"]} dport {dport} accept')
    for subnet in subnets:
        lines += [
            # Egress is explicit per protocol. Each /24 is also enforced by
            # the hub's WireGuard peer AllowedIPs, so a spoke cannot spoof
            # another spoke's subnet.
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip protocol tcp accept',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip protocol udp accept',
            f'    iifname "{transit_iface}" oifname "{wan_iface}" '
            f'ip saddr {subnet} ip protocol icmp '
            f'ct state established,related accept',
        ]
    lines.append('  }')
    return lines
