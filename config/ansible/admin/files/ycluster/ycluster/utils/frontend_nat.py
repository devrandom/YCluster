"""Frontend NAT exposure state and nftables rendering.

Desired VM -> public-port-range mappings are stored under
/cluster/frontend/nat/<proto>/<vm>/<first>-<last> (single ports are
<port>-<port>). The frontend nat-reconciler pulls the set and renders
an nftables ruleset; this module owns the data layer and the renderer.
"""

import json
import re
import sys

from ..common.etcd_utils import get_etcd_client

NAT_PREFIX = '/cluster/frontend/nat/'
PROTOS = ('tcp', 'udp')
TRANSIT_SUBNET = '100.100.0.0/24'

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


def _key(proto, vm, spec):
    first, last = _parse_ports(spec)
    return f'{NAT_PREFIX}{proto}/{vm}/{first}-{last}'


def validate_exposures(exposures):
    """Raise ValueError on overlaps between same-proto ranges."""
    by_proto = {}
    for e in exposures:
        first, last = _parse_ports(e['ports'])
        by_proto.setdefault(e['proto'], []).append((first, last, e))
    for proto, ranges in by_proto.items():
        ranges.sort()
        for (f1, l1, _), (f2, l2, _) in zip(ranges, ranges[1:]):
            if f2 <= l1:
                raise ValueError(
                    f'overlapping {proto} port ranges: '
                    f'{f1}-{l1} and {f2}-{l2}')
    return True


def list_exposures():
    """Return all exposure records as a sorted list of dicts."""
    client = get_etcd_client()
    out = []
    for val, meta in client.get_prefix(NAT_PREFIX):
        key = meta.key.decode()[len(NAT_PREFIX):]
        try:
            proto, vm, ports = key.split('/')
            record = json.loads(val.decode())
        except (ValueError, json.JSONDecodeError):
            continue
        out.append({'proto': proto, 'vm': vm, 'ports': ports,
                    'transit_ip': record.get('transit_ip', '')})
    return sorted(out, key=lambda e: (e['proto'], e['vm'], e['ports']))


def set_exposure(vm, proto, ports, transit_ip=None):
    """Idempotently store one exposure mapping; returns the etcd key."""
    if proto not in PROTOS:
        raise ValueError(f'proto must be one of {PROTOS}')
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', vm):
        raise ValueError(f'invalid vm name: {vm!r}')
    if transit_ip is not None and not re.match(
            r'^100\.100\.0\.[0-9]{1,3}$', transit_ip):
        raise ValueError(f'transit_ip must be in {TRANSIT_SUBNET}: {transit_ip!r}')
    key = _key(proto, vm, ports)
    record = {}
    if transit_ip:
        record['transit_ip'] = transit_ip
    get_etcd_client().put(key, json.dumps(record))
    return key


def delete_exposure(vm, proto, ports):
    """Delete one mapping. Raises ValueError on invalid input."""
    if proto not in PROTOS:
        raise ValueError(f'proto must be one of {PROTOS}')
    get_etcd_client().delete(_key(proto, vm, ports))


def render_nftables(exposures, public_ip, wan_iface='eth0',
                    transit_iface='transit0'):
    """Render a complete, deterministic nftables ruleset.

    Fail-closed: the table's forward chain policy is drop for every
    protocol. Only DNAT'd inbound ranges and SNAT'd transit egress are
    accepted. Anti-spoofing matches on iifname/oifname + source.
    """
    validate_exposures(exposures)
    lines = [
        'table ip ycluster_vm_nat {',
        '  chain prerouting {',
        '    type nat hook prerouting priority dstnat; policy accept;',
    ]
    for e in exposures:
        first, last = _parse_ports(e['ports'])
        if not e.get('transit_ip'):
            continue  # not yet allocated; quarantine until reconciler fills it
        dport = f'{first}' if first == last else f'{first}-{last}'
        lines.append(
            f'    iifname "{wan_iface}" {e["proto"]} dport {dport} '
            f'dnat to {e["transit_ip"]}')
    lines += [
        '  }',
        '  chain postrouting {',
        '    type nat hook postrouting priority srcnat; policy accept;',
        f'    ip saddr {TRANSIT_SUBNET} oifname "{wan_iface}" '
        f'snat ip to {public_ip}',
        '  }',
        '  chain forward {',
        '    type filter hook forward priority filter; policy drop;',
        '    ct state established,related accept',
        '    iifname "zts*" drop',
    ]
    for e in exposures:
        first, last = _parse_ports(e['ports'])
        if not e.get('transit_ip'):
            continue
        dport = f'{first}' if first == last else f'{first}-{last}'
        lines.append(
            f'    iifname "{wan_iface}" oifname "{transit_iface}" '
            f'ip daddr {e["transit_ip"]} {e["proto"]} dport {dport} accept')
    lines += [
        f'    iifname "{transit_iface}" oifname "{wan_iface}" '
        f'ip saddr {TRANSIT_SUBNET} ip daddr 10.0.0.0/8 drop',
        f'    iifname "{transit_iface}" oifname "{wan_iface}" '
        f'ip saddr {TRANSIT_SUBNET} accept',
        '  }',
        '}',
    ]
    return '\n'.join(lines) + '\n'


# Transit NIC allocation --------------------------------------------------
# Compute-side: attach a transit NIC to VMs that have exposures and
# record the allocated transit IP back into the etcd mapping records so
# the frontend agent can render DNAT rules. The transit bridge is a
# dedicated Incus network (created by the frontend-nat Ansible playbook);
# it is NOT incusbr0, so the host's catch-all masquerade does not apply.

TRANSIT_BRIDGE = 'transit0'
TRANSIT_NIC_DEVICE = 'transit0'
TRANSIT_POOL_START = 11  # .2-.10 reserved for infra (gateway, agent, ...)
TRANSIT_POOL_END = 254


def _incus(*args, **kwargs):
    from .vm_manager import _incus as run
    return run(*args, **kwargs)


def _transit_nics_by_vm():
    """Return {instance_name: transit_ip} from live `transit0` devices."""
    out = {}
    result = _incus('list', '--format', 'json', check=False)
    try:
        instances = json.loads(result.stdout)
    except ValueError:
        return out
    for inst in instances:
        ip = ((inst.get('devices') or {}).get(TRANSIT_NIC_DEVICE) or {}).get('ipv4.address')
        if ip:
            out[inst['name']] = ip
    return out


def sync_transit_nics(local_instances):
    """Attach transit NICs + allocate transit IPs for exposed local VMs.

    `local_instances` is the set of instance names on this host (the
    caller already has it from reconcile). For each exposure whose VM
    runs here and has no transit_ip yet, allocate the next free
    100.100.0.x, attach the NIC, and update every exposure record for
    that VM. Idempotent: already-allocated VMs are skipped.
    """
    exposures = list_exposures()
    by_vm = {}
    for e in exposures:
        by_vm.setdefault(e['vm'], []).append(e)

    allocated = {e['transit_ip'] for e in exposures if e.get('transit_ip')}
    used = {ip.rsplit('.', 1)[1] for ip in allocated}
    live = _transit_nics_by_vm()

    changed = False
    for vm, records in sorted(by_vm.items()):
        if vm not in local_instances:
            continue
        if records[0].get('transit_ip'):
            continue  # already allocated (idempotent)
        # Pin the device if it doesn't exist yet (e.g. first tick).
        if vm not in live:
            _incus('config', 'device', 'add', vm, TRANSIT_NIC_DEVICE,
                   'nic', f'network={TRANSIT_BRIDGE}', check=True)
        else:
            pass  # device exists; just need the IP in etcd
        # Re-read live IPs in case the device add above now shows one.
        live = _transit_nics_by_vm()
        ip = live.get(vm)
        if not ip:
            # Allocate the next free octet in the transit pool.
            for i in range(TRANSIT_POOL_START, TRANSIT_POOL_END + 1):
                candidate = f'100.100.0.{i}'
                if i not in {int(o) for o in used}:
                    ip = candidate
                    used.add(str(i))
                    break
            if not ip:
                print(f'Warning: no free transit IPs for {vm}',
                      file=sys.stderr)
                continue
            _incus('config', 'device', 'set', vm, TRANSIT_NIC_DEVICE,
                   f'ipv4.address={ip}')
        for e in records:
            set_exposure(e['vm'], e['proto'], e['ports'], ip)
            changed = True
    return changed
