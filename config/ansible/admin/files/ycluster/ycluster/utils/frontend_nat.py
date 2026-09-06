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
TRANSIT_SUPERNET = '100.100.0.0/16'
# Hosts provisioned before per-host subnets used this pool.
LEGACY_TRANSIT_SUBNET = '100.100.0.0/24'

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
    """Validate via the standalone renderer (delegates; see nat_render)."""
    from .nat_render import validate_exposures as _validate
    return _validate(exposures)


def _valid_transit_ip(value):
    """True if value is a valid IPv4 address in a transit data subnet."""
    import ipaddress
    try:
        return (ipaddress.ip_address(value) in
                ipaddress.ip_network(TRANSIT_SUPERNET, strict=False))
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
            f'transit_ip must be in {TRANSIT_SUPERNET}: {transit_ip!r}')
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
                    transit_iface='wg-vm-nat'):
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


def _configured_transit_subnet():
    """Return this host's transit /24, defaulting to the legacy pool."""
    from .nat_render import transit_subnet
    from .vm_manager import _host_config_value
    value = _host_config_value('vm_transit_subnet')
    return transit_subnet(value) if value else LEGACY_TRANSIT_SUBNET


def _transit_reservation_key(ip):
    """Return the per-subnet reservation key for `ip`.

    The legacy /24 keeps its old key shape so existing reservations remain
    valid through the upgrade; all other /24s are namespaced by subnet.
    """
    from .nat_render import transit_subnet
    subnet = transit_subnet(ip)
    if subnet == LEGACY_TRANSIT_SUBNET:
        return f'{NAT_PREFIX}_ip/{ip}'.encode()
    return f'{NAT_PREFIX}_ip/{subnet}/{ip}'.encode()


def _reserve_transit_ip(vm, subnet):
    """Transactionally reserve the next free transit IP for `vm`.

    Uses an etcd transaction (compare-version=0) so concurrent hosts
    cannot allocate the same octet to two VMs: only the first put on a
    given reservation key succeeds; losers retry the next candidate.
    Returns the reserved IP or None if the pool is exhausted.
    """
    import ipaddress
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    network = ipaddress.ip_network(subnet, strict=False)
    for i in range(TRANSIT_POOL_START, TRANSIT_POOL_END + 1):
        ip = str(network.network_address + i)
        key = _transit_reservation_key(ip)
        ok, _ = client.transaction(
            compare=[client.transactions.version(key) == 0],
            success=[client.transactions.put(key, vm.encode())],
        )
        if ok:
            return ip
    return None


def _claim_transit_ip(vm, ip):
    """Transactionally claim a specific transit IP for `vm` (CAS).

    Used when adopting a live device address that has no reservation
    key yet (partial write or manual attach). Returns True on success.
    """
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    key = _transit_reservation_key(ip)
    ok, _ = client.transaction(
        compare=[client.transactions.version(key) == 0],
        success=[client.transactions.put(key, vm.encode())],
    )
    return ok


def sync_transit_nics(local_instances, transit_subnet=None):
    """Attach transit NICs + allocate transit IPs for exposed local VMs.

    `local_instances` is the set of instance names on this host (the
    caller already has it from reconcile). For each exposure whose VM
    runs here and has no transit_ip yet, reserve an IP transactionally,
    attach the NIC, and update every exposure record for that VM.
    Live transit devices without a recorded IP are adopted
    transactionally (CAS on the reservation key) so two hosts cannot
    claim the same device for different VMs.
    """
    from .nat_render import transit_subnet as _subnet
    if transit_subnet is None:
        transit_subnet = _configured_transit_subnet()
    else:
        transit_subnet = _subnet(transit_subnet)

    exposures = list_exposures()
    by_vm = {}
    for e in exposures:
        by_vm.setdefault(e['vm'], []).append(e)

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
        if ip and _subnet(ip) != transit_subnet:
            print(f'Warning: {vm} transit IP is outside {transit_subnet}; '
                  'skipping', file=sys.stderr)
            continue
        if not ip:
            live = _transit_nics_by_vm().get(vm)
            if live:
                if _subnet(live) != transit_subnet:
                    print(f'Warning: {vm} live transit IP is outside '
                          f'{transit_subnet}; skipping', file=sys.stderr)
                    continue
                # Device exists but etcd has no IP recorded (partial
                # write or manual attach): adopt it transactionally so
                # no other host can claim the same address meanwhile.
                if not _claim_transit_ip(vm, live):
                    continue
                ip = live
            else:
                ip = _reserve_transit_ip(vm, transit_subnet)
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
