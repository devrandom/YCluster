"""Frontend NAT exposure state and nftables rendering.

Desired VM -> public-port-range mappings are stored under
/cluster/frontend/nat/<proto>/<vm>/<first>-<last> (single ports are
<port>-<port>). The frontend nat-reconciler pulls the set and renders
an nftables ruleset; this module owns the data layer and the renderer.
"""

import hashlib
import json
import re
import sys

# NOTE: get_etcd_client is imported lazily inside the functions that use
# it. The frontend nat-reconciler agent imports this module for
# render_nftables() and runs OUTSIDE the cluster envelope — it must not
# need the etcd3 client installed.

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


def _valid_transit_ip(value):
    """True if value is a valid IPv4 address inside TRANSIT_SUBNET."""
    import ipaddress
    try:
        return (ipaddress.ip_address(value) in
                ipaddress.ip_network(TRANSIT_SUBNET, strict=False))
    except ValueError:
        return False


def list_exposures():
    """Return all exposure records as a sorted list of dicts.

    Malformed keys or records are skipped (quarantined) so the
    reconciler never interpolates unvalidated data into nft syntax.
    """
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    out = []
    for val, meta in client.get_prefix(NAT_PREFIX):
        key = meta.key.decode()[len(NAT_PREFIX):]
        try:
            proto, vm, ports = key.split('/')
            record = json.loads(val.decode())
            if proto not in PROTOS:
                raise ValueError(proto)
            _parse_ports(ports)
            if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', vm):
                raise ValueError(vm)
            transit_ip = record.get('transit_ip', '')
            if transit_ip and not _valid_transit_ip(transit_ip):
                raise ValueError(transit_ip)
        except (ValueError, json.JSONDecodeError):
            continue
        out.append({'proto': proto, 'vm': vm, 'ports': ports,
                    'transit_ip': transit_ip})
    return sorted(out, key=lambda e: (e['proto'], e['vm'], e['ports']))


def set_exposure(vm, proto, ports, transit_ip=None):
    """Idempotently store one exposure mapping; returns the etcd key."""
    if proto not in PROTOS:
        raise ValueError(f'proto must be one of {PROTOS}')
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', vm):
        raise ValueError(f'invalid vm name: {vm!r}')
    if transit_ip is not None and not _valid_transit_ip(transit_ip):
        raise ValueError(
            f'transit_ip must be in {TRANSIT_SUBNET}: {transit_ip!r}')
    key = _key(proto, vm, ports)
    record = {}
    if transit_ip:
        record['transit_ip'] = transit_ip
    from ..common.etcd_utils import get_etcd_client
    get_etcd_client().put(key, json.dumps(record))
    return key


def delete_exposure(vm, proto, ports):
    """Delete one mapping. Raises ValueError on invalid input."""
    if proto not in PROTOS:
        raise ValueError(f'proto must be one of {PROTOS}')
    from ..common.etcd_utils import get_etcd_client
    get_etcd_client().delete(_key(proto, vm, ports))


def render_nftables(exposures, public_ip, wan_iface='eth0',
                    transit_iface='transit0'):
    """Delegate to the standalone renderer (see nat_render.py)."""
    from .nat_render import render_nftables as _render
    return _render(exposures, public_ip, wan_iface, transit_iface)

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


def _reserve_transit_ip(vm):
    """Transactionally reserve the next free transit IP for `vm`.

    Uses an etcd transaction (compare-version=0) so concurrent hosts
    cannot allocate the same octet to two VMs: only the first put on a
    given reservation key succeeds; losers retry the next candidate.
    Returns the reserved IP or None if the pool is exhausted.
    """
    import ipaddress
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    for i in range(TRANSIT_POOL_START, TRANSIT_POOL_END + 1):
        ip = str(ipaddress.IPv4Address(f'100.100.0.{i}'))
        key = f'{NAT_PREFIX}_ip/{ip}'.encode()
        ok, _ = client.transaction(
            compare=[client.transactions.version(key) == 0],
            success=[client.transactions.put(key, vm.encode())],
        )
        if ok:
            return ip
    return None


def sync_transit_nics(local_instances):
    """Attach transit NICs + allocate transit IPs for exposed local VMs.

    `local_instances` is the set of instance names on this host (the
    caller already has it from reconcile). For each exposure whose VM
    runs here and has no transit_ip yet, reserve an IP transactionally,
    attach the NIC, and update every exposure record for that VM.
    Idempotent: already-allocated VMs are converged (missing records on
    a VM whose first record has an IP are filled in), and live transit
    devices on unrelated VMs are treated as used so stale devices
    cannot collide with fresh allocations.
    """
    exposures = list_exposures()
    by_vm = {}
    for e in exposures:
        by_vm.setdefault(e['vm'], []).append(e)

    # Live transit devices count as used so a stale device (VM deleted
    # but NIC lingering) blocks its IP from being handed to a new VM.
    live = _transit_nics_by_vm()
    used_ips = {e['transit_ip'] for e in exposures if e.get('transit_ip')}
    used_ips.update(live.values())

    changed = False
    for vm, records in sorted(by_vm.items()):
        if vm not in local_instances:
            continue
        existing = {r.get('transit_ip') for r in records} - {''}
        if len(existing) > 1:
            print(f'Warning: {vm} has conflicting transit IPs; skipping',
                  file=sys.stderr)
            continue
        ip = existing.pop() if existing else None
        if not ip:
            if vm in live:
                # Device exists but etcd has no IP recorded (partial
                # write or manual attach); adopt the live address.
                ip = live[vm]
            else:
                ip = _reserve_transit_ip(vm)
                if not ip:
                    print(f'Warning: no free transit IPs for {vm}',
                          file=sys.stderr)
                    continue
                _incus('config', 'device', 'add', vm, TRANSIT_NIC_DEVICE,
                       'nic', f'network={TRANSIT_BRIDGE}',
                       f'ipv4.address={ip}', check=True)
        # Converge every record for this VM (partial writes happen when
        # a tick dies between per-record puts).
        for e in records:
            if e.get('transit_ip') != ip:
                set_exposure(e['vm'], e['proto'], e['ports'], ip)
                changed = True
    return changed
