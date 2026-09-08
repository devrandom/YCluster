"""Static IP pinning and dnsmasq DNS host-records for Incus instances.

Split out of vm_manager.py: the pinning/DNS reconciliation is a
self-contained surface with its own tests (tests/test_ip_pinning.py).
"""

import json
import subprocess

# Per-host incus bridge subnet (10.100.0.0/24). VMs and containers get a
# pinned IP from this range so dnsmasq can never hand the same address to
# two long-running guests. The pool excludes the bridge gateway (.1), the
# bastion (left to dnsmasq's first-free pick), and a small cushion at the top.
VM_IP_SUBNET_PREFIX = "10.100.0"
VM_IP_POOL_START = 10
VM_IP_POOL_END = 200
BASTION_CONTAINER = "bastion"


def _incus(*args, check=True, stdin=None, timeout=None):
    # The incus CLI reads instance config from a non-TTY stdin until EOF,
    # so it must never inherit ours (`ssh host 'ycluster vm launch ...'`
    # keeps stdin open and incus init blocks forever).
    if stdin is not None:
        r = subprocess.run(
            ["incus", *args], input=stdin, text=True, capture_output=True,
            timeout=timeout,
        )
    else:
        r = subprocess.run(
            ["incus", *args], stdin=subprocess.DEVNULL, text=True,
            capture_output=True, timeout=timeout,
        )
    if check and r.returncode != 0:
        raise RuntimeError(f"incus {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def _incus_json(*args):
    """`incus list --format json` and friends, parsed."""
    r = _incus(*args)
    return json.loads(r.stdout)


__all__ = ["pin_existing_vms", "sync_dns_records", "_pick_vm_ip",
           "_pin_instance_ip", "_pinned_ips", "_live_ipv4"]


def _pinned_ips():
    """Return {instance_name: ipv4.address} for instances whose eth0 nic
    device has a static IP assigned (via instance-level override or via the
    profile they inherit from)."""
    pinned = {}
    for inst in _incus_json("list", "--format", "json"):
        eth0 = (inst.get("expanded_devices") or {}).get("eth0") or {}
        ip = eth0.get("ipv4.address")
        if ip:
            pinned[inst["name"]] = ip
    return pinned

def _occupied_ips():
    """Pinned eth0 addresses plus every currently-live instance address."""
    occupied = set(_pinned_ips().values())
    return occupied.union(*(_live_instance_ips(i) for i in _incus_json("list", "--format", "json")))

def _live_instance_ips(inst):
    net = (inst.get("state") or {}).get("network") or {}
    return {a["address"]
            for iface in net.values()
            for a in iface.get("addresses", []) or []
            if a.get("family") == "inet" and a.get("scope") == "global"}

def _pick_vm_ip(prefer=None, taken=None):
    """Return the first unused IP in VM_IP_POOL_START..VM_IP_POOL_END.

    If `prefer` is supplied and not currently taken by another instance,
    return it instead (used by `pin_existing_vms` to preserve a running
    guest's address across the migration). `taken` lets callers pass an
    accumulating set so successive picks within one operation don't
    collide.
    """
    if taken is None:
        taken = _occupied_ips()
    else:
        taken = set(taken)
    if prefer and prefer not in taken:
        return prefer
    for i in range(VM_IP_POOL_START, VM_IP_POOL_END + 1):
        ip = f"{VM_IP_SUBNET_PREFIX}.{i}"
        if ip not in taken:
            return ip
    raise RuntimeError(
        f"No free IPs in {VM_IP_SUBNET_PREFIX}.{VM_IP_POOL_START}–"
        f"{VM_IP_POOL_END}; taken: {sorted(taken)}"
    )

def _pin_instance_ip(name, ip):
    """Pin `name`'s eth0 nic to `ip`. Idempotent.

    Uses `device override` (which creates an instance-local copy of the
    profile device with the new key) the first time, and `device set`
    afterwards (override fails once the device is already overridden).
    Takes effect immediately for new devices; existing running guests
    pick up the change on next DHCP renewal (≈1h) or instance restart.
    """
    ov = _incus("config", "device", "override", name, "eth0",
                f"ipv4.address={ip}", check=False)
    if ov.returncode != 0:
        _incus("config", "device", "set", name, "eth0",
               f"ipv4.address={ip}")

def _managed_bridges():
    return [n["name"] for n in _incus_json("network", "list", "--format", "json")
            if n.get("type") == "bridge" and n.get("managed")]

def sync_dns_records():
    """Reconcile each managed bridge's dnsmasq host-records with the
    instances' eth0 IP pins.

    A pinned `ipv4.address` reserves the address in dnsmasq but creates no
    DNS record: dnsmasq only answers for an instance's name while the guest
    holds a live lease, so a guest that lets its lease lapse between
    renewals drops out of DNS and name-based SSH via the bastion breaks
    intermittently. A `host-record` line is static — it resolves regardless
    of lease state.

    raw.dnsmasq ownership is split by line type: this function owns every
    `host-record=` line (regenerated from the pins, so hand-added
    host-records are overwritten); admin/install-incus.yml owns the
    resolver lines and preserves host-records.

    Returns {bridge: [host-record lines]} for the bridges that changed.
    """
    by_bridge = {}
    for inst in _incus_json("list", "--format", "json"):
        eth0 = (inst.get("expanded_devices") or {}).get("eth0") or {}
        ip, bridge = eth0.get("ipv4.address"), eth0.get("network")
        if ip and bridge:
            by_bridge.setdefault(bridge, []).append((inst["name"], ip))

    changed = {}
    for bridge in _managed_bridges():
        domain = (_incus("network", "get", bridge, "dns.domain").stdout.strip()
                  or "incus")
        records = sorted(f"host-record={name},{name}.{domain},{ip}"
                         for name, ip in by_bridge.get(bridge, []))
        current = [line for line in
                   _incus("network", "get", bridge, "raw.dnsmasq")
                   .stdout.splitlines() if line]
        desired = [line for line in current
                   if not line.startswith("host-record=")] + records
        if desired != current:
            _incus("network", "set", bridge, "raw.dnsmasq",
                   "\n".join(desired))
            changed[bridge] = records
    return changed


def _live_ipv4(inst):
    """Return the instance's current live global IPv4, or None."""
    net = (inst.get("state") or {}).get("network") or {}
    for iface in net.values():
        for a in iface.get("addresses", []) or []:
            if a.get("family") == "inet" and a.get("scope") == "global":
                return a["address"]
    return None


def _live_ip_counts(insts):
    """Map live IP -> occurrence count, to detect collisions."""
    counts = {}
    for inst in insts:
        ip = _live_ipv4(inst)
        if ip:
            counts[ip] = counts.get(ip, 0) + 1
    return counts


def _pin_queues(insts, pinned_by_name, live_counts):
    """Split unpinned instances into unique-live-IP first, colliding later."""
    queue_first = []
    queue_later = []
    for inst in insts:
        name = inst["name"]
        if name == BASTION_CONTAINER or name in pinned_by_name:
            continue
        ip = _live_ipv4(inst)
        if ip and live_counts.get(ip, 0) == 1:
            queue_first.append(name)
        else:
            queue_later.append(name)
    return queue_first, queue_later


def pin_existing_vms():
    """Migration helper: pin a static IP on every non-bastion incus
    instance on this host that doesn't already have one.

    Strategy:
      - For each instance, prefer its currently-live address (so a running
        guest keeps its address through the migration) unless that address
        collides with another instance's pin/live IP.
      - Otherwise allocate the lowest free address from the VM IP pool.

    Pin takes effect immediately for the device record; the running guest
    keeps its current cached IP until the DHCP lease renews (≈1h with the
    default lease) or the instance is restarted. Subsequent renewals will
    receive the pinned address even after a long lease lapse.
    """
    insts = _incus_json("list", "--format", "json")
    pinned_by_name = _pinned_ips()
    taken = set(pinned_by_name.values())

    live = {inst["name"]: _live_ipv4(inst) for inst in insts}
    live_counts = _live_ip_counts(insts)

    # Pin instances whose live IP is *uniquely theirs* first, so the lucky
    # tie-winner gets to keep it before we reallocate the others.
    queue_first, queue_later = _pin_queues(insts, pinned_by_name, live_counts)

    changes = []
    for name in queue_first + queue_later:
        target = _pick_vm_ip(prefer=live[name], taken=taken)
        if target == live[name]:
            note = "kept current"
        elif live[name]:
            note = f"was {live[name]} (collision)"
        else:
            note = "was n/a (stopped)"
        _pin_instance_ip(name, target)
        taken.add(target)
        changes.append((name, target, note))
    sync_dns_records()
    return changes
