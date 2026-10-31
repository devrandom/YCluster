"""Frontend NAT exposure state and nftables rendering.

Desired VM -> public-port-range mappings are stored under
/cluster/frontend/nat/<proto>/<vm>/<first>-<last> (single ports are
<port>-<port>). The frontend nat-reconciler pulls the set and renders
an nftables ruleset; this module owns the data layer and the renderer.
"""

import hashlib
import json
import logging
import re
import shlex
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
_LOG = logging.getLogger(__name__)


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
    """True if value is a usable transit host address.

    The gateway (.1), infra range (.2-.10), network and broadcast
    addresses are excluded so a manually-exposed record cannot DNAT to
    them; the reconciler only ever allocates 11-254.
    """
    import ipaddress
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if addr not in ipaddress.ip_network(TRANSIT_SUPERNET, strict=False):
        return False
    return 11 <= int(addr) & 0xFF <= 254


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
        if key.startswith('_ip/'):
            continue  # reservation keys, not exposure records
        try:
            proto, vm, ports = key.split('/')
            record = json.loads(val.decode())
            if not isinstance(record, dict):
                raise ValueError('record must be a JSON object')
            if proto not in PROTOS:
                raise ValueError(proto)
            _parse_ports(ports)
            if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', vm):
                raise ValueError(vm)
            transit_ip = record.get('transit_ip', '')
            if transit_ip and not _valid_transit_ip(transit_ip):
                raise ValueError(transit_ip)
        except (ValueError, json.JSONDecodeError) as exc:
            _LOG.warning('Quarantined frontend NAT record %s: %s', key, exc)
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
SHADOW_BRIDGE = 'eth0-shadow'
BASTION_CONTAINER = 'bastion'
TRANSIT_HOSTS_BEGIN = '# ycluster-transit begin'
TRANSIT_HOSTS_END = '# ycluster-transit end'
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
            failure=[],
        )
        if ok:
            return ip
    return None


def _claim_transit_ip(vm, ip):
    """Transactionally claim a specific transit IP for `vm` (CAS).

    Used when adopting a live device address. Returns True when the
    claim succeeded or the reservation already belongs to this VM (a
    prior partial run created it); False when another VM holds it.
    """
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    key = _transit_reservation_key(ip)
    ok, _ = client.transaction(
        compare=[client.transactions.version(key) == 0],
        success=[client.transactions.put(key, vm.encode())],
        failure=[],
    )
    if ok:
        return True
    existing, _ = client.get(key)
    return existing is not None and existing.decode() == vm


def _release_transit_ip(ip):
    """Delete a reservation once a legacy address has been migrated."""
    from ..common.etcd_utils import get_etcd_client
    get_etcd_client().delete(_transit_reservation_key(ip))


def _gc_transit_reservations(exposures):
    """Delete reservations for VMs with no remaining exposure records.

    This is a coarse safety net for decommissioned mappings; active VMs
    keep their reservations even if their current record has no IP yet.
    """
    active_vms = {e['vm'] for e in exposures}
    from ..common.etcd_utils import get_etcd_client
    client = get_etcd_client()
    for val, meta in client.get_prefix(NAT_PREFIX + '_ip/'):
        try:
            key = meta.key.decode()
            vm = val.decode()
            ip = key.rsplit('/', 1)[-1]
        except (UnicodeDecodeError, ValueError):
            continue
        if (vm and re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', vm) and
                vm not in active_vms and _valid_transit_ip(ip)):
            client.delete(key.encode())


def _instance_details(vm):
    """Return the first `incus list` record for `vm`, or {}."""
    result = _incus('list', vm, '--format', 'json', check=False)
    try:
        instances = json.loads(result.stdout)
    except ValueError:
        return {}
    return instances[0] if instances else {}


NETPLAN_TRANSIT_DROPIN = "90-transit.yaml"
NETPLAN_TRANSIT_CONFIG = (
    "network:\n"
    "  version: 2\n"
    "  ethernets:\n"
    "    all-eth:\n"
    "      match:\n"
    "        name: \"en*\"\n"
    "      dhcp4: true\n"
    "      dhcp4-overrides:\n"
    "        route-metric: 100\n"
)


def _ensure_guest_transit_netplan(vm):
    """Make the guest DHCP on every ethernet interface.

    Base images commonly netplan-match only the first NIC, so a hot-added
    transit NIC stays down and the guest is unreachable. Idempotently
    drop in a wildcard-match config and apply it; no-op when the drop-in
    is already present (re-running netplan apply on every tick would be
    disruptive).
    """
    r = _incus("exec", vm, "--", "cat",
               f"/etc/netplan/{NETPLAN_TRANSIT_DROPIN}", check=False)
    existing = getattr(r, "stdout", None)
    if existing is not None and r.returncode == 0 \
            and existing == NETPLAN_TRANSIT_CONFIG:
        return False
    _incus("exec", vm, "--", "sh", "-c",
           f"printf '%s' {shlex.quote(NETPLAN_TRANSIT_CONFIG)} > "
           f"/etc/netplan/{NETPLAN_TRANSIT_DROPIN} && "
           f"chmod 600 /etc/netplan/{NETPLAN_TRANSIT_DROPIN}",
           check=True)
    return True


def _apply_guest_netplan(vm):
    """Apply netplan inside the guest."""
    _incus("exec", vm, "--", "netplan", "apply", check=True)


def _disable_default_nic(vm, details=None):
    """Neutralize the primary NIC for transit-only VMs.

    An inherited `eth0` would still give the guest a route into the
    cluster LAN. Incus has no `type=none` device, so the device is
    overridden onto a dead-end shadow bridge (no gateway, no uplink),
    even when the profile later changes.
    """
    details = details or _instance_details(vm)
    eth0 = (details.get('expanded_devices') or {}).get('eth0') or {}
    if eth0.get('network') == SHADOW_BRIDGE:
        return False
    # A local eth0 (pinned IP, filtering options) would fail override
    # validation, so remove it first; the override then re-creates it
    # detached from the cluster bridge.
    _incus('config', 'device', 'remove', vm, 'eth0', check=False)
    try:
        _incus('config', 'device', 'override', vm, 'eth0',
               f'network={SHADOW_BRIDGE}', check=True)
    except Exception:
        _incus('config', 'device', 'add', vm, 'eth0', 'nic',
               f'network={SHADOW_BRIDGE}', check=True)
    return True


def _restart_if_running(vm, details):
    """Restart once so a running guest cannot keep the old NIC/route."""
    if (details.get('status') or '').casefold() == 'running':
        _incus('restart', vm, check=True)


def _set_transit_address(vm, ip):
    _incus('config', 'device', 'set', vm, TRANSIT_NIC_DEVICE,
           f'ipv4.address={ip}', check=True)


def _converge_records(records, ip):
    """Update every record for one VM to `ip`; returns True when changed."""
    changed = False
    for e in records:
        if e.get('transit_ip') != ip:
            set_exposure(e['vm'], e['proto'], e['ports'], ip)
            e['transit_ip'] = ip
            changed = True
    return changed


def _remove_transit_device(vm, details):
    """Remove the transit NIC and restore any transit-only override."""
    _incus('config', 'device', 'remove', vm, TRANSIT_NIC_DEVICE,
           check=True)
    eth0 = (details.get('expanded_devices') or {}).get('eth0') or {}
    if eth0.get('network') == SHADOW_BRIDGE:
        # Drop the shadow override so the profile NIC is inherited again.
        _incus('config', 'device', 'remove', vm, 'eth0', check=True)


def _decommission_transit_nic(vm, ip):
    """Remove transit-only wiring after all mappings for a VM are gone."""
    details = _instance_details(vm)
    _remove_transit_device(vm, details)
    _release_transit_ip(ip)
    _restart_if_running(vm, details)
    return True


def _subnet_for(value):
    """Return the /24 containing a transit IP or CIDR."""
    from .nat_render import transit_subnet
    return transit_subnet(value)


def _migrate_legacy_ip(vm, records, old_ip, live, transit_subnet):
    """Re-address one legacy record set; returns the new IP or None."""
    if live and _subnet_for(live) == transit_subnet:
        if not _claim_transit_ip(vm, live):
            return None
        return live
    new_ip = _reserve_transit_ip(vm, transit_subnet)
    if not new_ip:
        print(f'Warning: no free transit IPs to migrate {vm}',
              file=sys.stderr)
        return None
    try:
        if live:
            _set_transit_address(vm, new_ip)
        else:
            # No live NIC (partial teardown or manual removal): create it
            # at the new address rather than setting an address on an
            # absent device, which would strand the migration forever.
            _incus('config', 'device', 'add', vm, TRANSIT_NIC_DEVICE, 'nic',
                   f'network={TRANSIT_BRIDGE}', f'ipv4.address={new_ip}',
                   check=True)
        # Close the cluster-side path before the frontend can route to the
        # new address; publishing records first would leave a window where
        # the VM is reachable via both eth0 and the new transit IP.
        _disable_default_nic(vm)
        _converge_records(records, new_ip)
    except Exception:
        _release_transit_ip(new_ip)
        raise
    _release_transit_ip(old_ip)
    return new_ip


def _allocate_transit_ip(vm, records, live, transit_subnet):
    """Reserve an address and attach the transit NIC."""
    ip = _reserve_transit_ip(vm, transit_subnet)
    if not ip:
        print(f'Warning: no free transit IPs for {vm}', file=sys.stderr)
        return None
    try:
        _incus('config', 'device', 'add', vm, TRANSIT_NIC_DEVICE, 'nic',
               f'network={TRANSIT_BRIDGE}', f'ipv4.address={ip}', check=True)
    except Exception:
        _release_transit_ip(ip)
        raise
    return ip


def _reconcile_device(vm, ip, live, attached):
    """Point the transit NIC at `ip`; returns True when changed.

    Called with the converged record IP after allocation/migration. A
    live NIC holding a different address is re-set to the reserved one;
    a missing NIC is re-created. Returns False when nothing is needed.
    """
    if live and live != ip:
        # Records agree but the live NIC holds a different address in
        # this subnet (manual change or stale device). Converge the
        # device to the recorded address, which is the reserved one,
        # rather than leaving records and device permanently diverged.
        _set_transit_address(vm, ip)
        return True
    if not live and not attached:
        # A recorded IP with no live NIC (partial teardown or manual
        # removal): re-attach transit0, otherwise the eth0 type=none
        # override below would strand the guest with no network path.
        try:
            _incus('config', 'device', 'add', vm, TRANSIT_NIC_DEVICE,
                   'nic', f'network={TRANSIT_BRIDGE}',
                   f'ipv4.address={ip}', check=True)
        except Exception as exc:
            print(f'Warning: failed to reattach transit NIC for {vm}: '
                  f'{exc}', file=sys.stderr)
            return None
        return True
    return False


def _recorded_transit_ip(vm, records, live):
    """Return the IP recorded for `vm`, or `(None, False)` when unreadable.

    Records disagree after a partial migration or write. The live NIC is
    ground truth for what the guest actually holds, so converge every record
    onto it rather than guessing and stranding the VM.
    """
    existing = {r.get('transit_ip') for r in records} - {''}
    if len(existing) <= 1:
        return (existing.pop() if existing else None), True
    if not live:
        print(f'Warning: {vm} has conflicting transit IPs and no live '
              'device; skipping', file=sys.stderr)
        return None, False
    print(f'Warning: {vm} has conflicting transit IPs; resolving to '
          f'the live device {live}', file=sys.stderr)
    return live, True


def _converge_vm(vm, records, live, transit_subnet):
    """Converge one VM; returns `(ip, device_changed)` or `(None, False)`."""
    details = _instance_details(vm)
    device_changed = False
    attached = False
    ip, ok = _recorded_transit_ip(vm, records, live)
    if not ok:
        return None, False
    if ip and _subnet_for(ip) != transit_subnet:
        migrated = _migrate_legacy_ip(vm, records, ip, live, transit_subnet)
        if not migrated:
            return None, False
        ip = migrated
        attached = True
        device_changed = True
    if not ip:
        if live and _subnet_for(live) != transit_subnet:
            migrated = _migrate_legacy_ip(vm, records, live, live,
                                          transit_subnet)
            if not migrated:
                return None, False
            ip = migrated
            attached = True
            device_changed = True
        elif live:
            # Device exists but etcd has no IP recorded (partial write or
            # manual attach): adopt it transactionally so no other host
            # can claim it meanwhile.
            if not _claim_transit_ip(vm, live):
                return None, False
            ip = live
        else:
            ip = _allocate_transit_ip(vm, records, live, transit_subnet)
            if not ip:
                return None, False
            attached = True
            device_changed = True
    reconciled = _reconcile_device(vm, ip, live, attached)
    if reconciled is None:
        return None, False
    device_changed |= reconciled
    # Exposed VMs are transit-only. An inherited primary NIC would
    # otherwise leave a usable default route into cluster 10.* space.
    if _disable_default_nic(vm, details):
        device_changed = True
    # Guests commonly netplan-match only their first NIC, so the hot-added
    # transit NIC stays down. Ensure the wildcard drop-in exists (idempotent
    # probe; cheap) and apply/restart when anything changed. A stopped guest
    # cannot be exec'd into; retrying every reconcile tick just produces a
    # failed API call, so defer the guest-side work until it is running.
    netplan_changed = False
    status = str(details.get('status', '')).lower()
    if not status or status == 'running':
        netplan_changed = _ensure_guest_transit_netplan(vm)
    if device_changed or netplan_changed:
        if netplan_changed:
            _apply_guest_netplan(vm)
        _restart_if_running(vm, details)
    return ip, device_changed or netplan_changed


def _replace_hosts_block(current, entries):
    """Return /etc/hosts content with the transit block set to `entries`.

    `entries` is an iterable of `(vm, ip)` pairs. The managed block is
    replaced in place when present and appended otherwise.
    """
    block = [TRANSIT_HOSTS_BEGIN]
    block += [f'{ip} {vm}' for vm, ip in sorted(entries, key=lambda e: e[1])]
    block.append(TRANSIT_HOSTS_END)
    lines = (current or '').splitlines()
    try:
        b = lines.index(TRANSIT_HOSTS_BEGIN)
        e = lines.index(TRANSIT_HOSTS_END, b + 1)
        kept = lines[:b] + block + lines[e + 1:]
    except ValueError:
        kept = lines + [''] + block if lines and lines[-1].strip() else lines + block
    return '\n'.join(kept) + '\n'


def _instance_exists(name):
    from .vm_manager import _instance_exists as exists
    return exists(name)


def _read_file(inst, path):
    from .vm_manager import _read_file as read
    return read(inst, path)


def _push_file(inst, path, content, owner, mode="0600"):
    from .vm_manager import _push_file as push
    push(inst, path, content, owner, mode)


def _sync_bastion_hosts(ip_by_vm):
    """Point the local bastion's name resolution at transit IPs.

    `ssh -J jump@... <vm>` resolves `<vm>` on the bastion; Incus dnsmasq
    answers with the VM-bridge address, which a transit-only guest no
    longer has. Keep a managed /etc/hosts block on the bastion so exposed
    VM names resolve to their transit addresses instead.
    """
    if not _instance_exists(BASTION_CONTAINER):
        return False
    hosts_path = '/etc/hosts'
    current = _read_file(BASTION_CONTAINER, hosts_path) or ''
    updated = _replace_hosts_block(current, ip_by_vm.items())
    if updated == current:
        return False
    # Not _push_file: it hardens the parent directory to 0700, which is
    # right for .ssh but would lock non-root users out of /etc.
    _incus('exec', BASTION_CONTAINER, '--', 'tee', hosts_path,
           stdin=updated, check=True)
    return True


def sync_transit_nics(local_instances, transit_subnet=None):
    """Attach transit NICs + allocate transit IPs for exposed local VMs.

    `local_instances` is the set of instance names on this host (the
    caller already has it from reconcile). For each exposure whose VM
    runs here and has no transit_ip yet, reserve an IP transactionally,
    attach the NIC, and update every exposure record for that VM.
    Live transit devices without a recorded IP are adopted
    transactionally (CAS on the reservation key) so two hosts cannot
    claim the same device for different VMs.

    Legacy addresses are re-addressed into this host's configured /24.
    The primary NIC is overridden with `type=none` so an exposed VM can
    only use transit0 for forwarding; running guests are restarted after
    a NIC change. VMs with no remaining exposures are decommissioned
    from transit0 and their reservations are garbage-collected.
    """
    from .nat_render import transit_subnet as _subnet
    if transit_subnet is None:
        transit_subnet = _configured_transit_subnet()
    else:
        transit_subnet = _subnet(transit_subnet)

    exposures = list_exposures()
    _gc_transit_reservations(exposures)
    by_vm = {}
    for e in exposures:
        by_vm.setdefault(e['vm'], []).append(e)
    live_by_vm = _transit_nics_by_vm()

    changed = False
    ip_by_vm = {}
    for vm, ip in sorted(live_by_vm.items()):
        if vm in local_instances and vm not in by_vm:
            try:
                changed |= _decommission_transit_nic(vm, ip)
            except Exception as exc:
                print(f'Warning: transit cleanup failed for {vm}: {exc}',
                      file=sys.stderr)

    for vm, records in sorted(by_vm.items()):
        if vm not in local_instances:
            continue
        try:
            ip, device_changed = _converge_vm(
                vm, records, live_by_vm.get(vm), transit_subnet)
            if ip is None:
                continue
            ip_by_vm[vm] = ip
            changed |= device_changed
            changed |= _converge_records(records, ip)
        except Exception as exc:
            print(f'Warning: transit NIC sync failed for {vm}: {exc}',
                  file=sys.stderr)
            continue
    try:
        changed |= _sync_bastion_hosts(ip_by_vm)
    except Exception as exc:
        print(f'Warning: bastion hosts sync failed: {exc}', file=sys.stderr)
    return changed
