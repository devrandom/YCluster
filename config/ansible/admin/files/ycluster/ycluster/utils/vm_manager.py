"""GPU VM hosting on Incus.

Manages GPU-passthrough VMs, a per-user SSH key registry, a VM-ownership
registry, and the SSH jump bastion's access list. Runs on the Incus host:
shells out to the local `incus` client and reads/writes etcd.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from ..common.etcd_utils import get_etcd_client, reset_etcd_client

USERS_PREFIX = "/cluster/users/"
VMS_PREFIX = "/cluster/vms/"
# Lifecycle event queue, drained into usage_stats by collect-vm-stats on
# the storage leader. Deliberately NOT under VMS_PREFIX — vm_list/vms_all
# iterate that prefix and would mistake events for registrations.
EVENTS_PREFIX = "/cluster/vms-events/"
# Per-host incus state snapshots, written by the vm-state-sampler timer on
# incus hosts (`ycluster vm sample`) and read by collect-vm-stats. Push via
# etcd client certs — the cluster's one trust mechanism — rather than the
# leader reaching into hosts (ssh/HTTP).
VM_STATE_PREFIX = "/cluster/vm-state/"
# Stamped into each snapshot; must match the vm-state-sampler.timer cadence
# (files/vm-state-sampler.timer) — observed GPU-hours = sum(gpus * interval_s).
VM_STATE_INTERVAL_S = 60
# Desired power state per VM, written by the scheduling page (admin-api on
# the leader) and converged by the vm-reconciler timer on each incus host —
# hosts pull intent and push state over etcd client certs; there is no
# inbound control channel. {"mode": "on"|"off"|"schedule", "windows":
# [{"start": <ISO datetime>, "end": <ISO datetime>}] (UTC; one-shot
# absolute windows, not recurring; multiple allowed),
# "updated_by": ..., "updated_at": ...}. No record = unmanaged (status quo).
VM_DESIRED_PREFIX = "/cluster/vm-desired/"
# Scheduler stop-grace markers ({"warned_at": iso}). Tick-based: one
# reconcile tick warns the guest and stamps the marker, a later tick
# (>= the grace below after the warning) stops — no in-process sleeps,
# so concurrent stops don't serialize and a hung guest can't stall the
# reconciler.
VM_GRACE_PREFIX = "/cluster/vm-grace/"
# Minimum warning-to-shutdown grace for scheduler-initiated stops
# (effective grace is this rounded up to the next reconciler tick).
SCHEDULED_STOP_GRACE_S = 300
# Last reconciler failure per VM ({"op": "start"|"stop", "error": ...,
# "at": iso}), written when convergence fails and cleared once the VM
# converges (or its desired record disappears). Read by the schedule page
# so start failures (e.g. RAM exhaustion) surface to the owner, not only
# in the reconciler journal.
VM_ISSUE_PREFIX = "/cluster/vm-issue/"

# Admin GPU reservations ("blocks") — a schedule entry with no backing VM.
# An admin reserves `gpus` on `host` so the scheduler's admission control
# treats them as committed capacity, exactly as if a VM were holding them,
# without provisioning anything. Purely accounting/admission — there is no
# physical GPU to attach, so nothing converges them. Records look like:
#   {"host": "nv2", "gpus": 2, "mode": "on"|"schedule",
#    "windows": [{start, end}, ...],  # required+valid for 'schedule'
#    "reason": ..., "created_by": ..., "created_at": ..., "updated_at": ...}
# Block ids are server-generated and never collide with VM names.
GPU_BLOCKS_PREFIX = "/cluster/gpu-blocks/"

VM_PROFILE = "gpu-vm"
VM_IMAGE = "ubuntu-cuda-vllm"          # default for VMs (GPU or not):
                                       # ubuntu-cuda + vLLM + FlashInfer AOT
                                       # cache. Pass --image ubuntu-cuda to
                                       # skip the vLLM layer (~1 GiB lighter).
CT_PROFILE = "gpu-ct"
CT_IMAGE = "ubuntu-rocm"               # default for containers: Ubuntu
                                       # 24.04 + ROCm HIP runtime + render
                                       # group / ubuntu membership matching
                                       # the host's render gid (built by
                                       # incus-build-rocm-image). Pass
                                       # --image images:ubuntu/24.04 to
                                       # use the plain upstream base.
VM_GUEST_USER = "ubuntu"               # default user in the Ubuntu cloud image
BASTION_CONTAINER = "bastion"
BASTION_JUMP_USER = "jump"
HOST_CONFIG_PATH = "/etc/ycluster/host.yml"

# Per-host incus bridge subnet (10.100.0.0/24). VMs and containers get a
# pinned IP from this range so dnsmasq can never hand the same address to
# two long-running guests (the lease-prune-while-still-running scenario
# that previously caused VM↔VM IP collisions). The pool excludes the
# bridge gateway (.1), the bastion (left to dnsmasq's first-free pick),
# and a small cushion at the top.
VM_IP_SUBNET_PREFIX = "10.100.0"
VM_IP_POOL_START = 10
VM_IP_POOL_END = 200

# NAS share: each instance gets a dedicated subdir under /near1/vm/<name>
# on the host, attached as /data inside the guest. virtiofsd sandboxes the
# export, so the guest cannot escape to sibling VMs' subdirs or to other
# parts of /near1. All VMs still write to NAS as the same Samba user (the
# host's CIFS mount pins uid=1000,gid=1000) — isolation is at the virtiofs
# export boundary, not at NAS ACL level.
NAS_HOST_BASE = "/near1/vm"
NAS_GUEST_PATH = "/data"
NAS_DEVICE = "data"

_VM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")     # Incus instance names
_USER_RE = re.compile(r"^[A-Za-z0-9._%+@-]{1,128}$")        # identifiers / emails

# --------------------------------------------------------------------------
# etcd helpers
# --------------------------------------------------------------------------
def _get_json(key):
    val, _ = get_etcd_client().get(key)
    return json.loads(val.decode()) if val else None

def _put_json(key, obj):
    get_etcd_client().put(key, json.dumps(obj))

def _delete(key):
    get_etcd_client().delete(key)

def _all_json(prefix):
    """Return {name: record} for direct children of an etcd prefix."""
    out = {}
    for val, meta in get_etcd_client().get_prefix(prefix):
        name = meta.key.decode()[len(prefix):]
        if not name or "/" in name:
            continue
        out[name] = json.loads(val.decode())
    return out

# --------------------------------------------------------------------------
# incus helpers
# --------------------------------------------------------------------------
# Bounded stop windows so a wedged guest can never pin the reconciler.
# Containers get a shorter graceful window — their stop is just SIGPWR to
# PID1, and if that hangs we fall back to --force (killable userspace).
# VMs are given longer to run the guest shutdown, and are NEVER forced
# (SIGKILLing qemu mid-GPU-reset can wedge the vfio device in-kernel).
_STOP_TIMEOUT_CONTAINER_S = 90
_STOP_TIMEOUT_VM_S = 180
_STOP_TIMEOUT_FORCE_S = 30

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

def _incus_stop(name):
    """Stop `name` with a bounded wait; never hang on a wedged guest.

    Graceful `incus stop` first, with a per-type timeout. If a container
    times out (PID1 stuck in userspace) fall back to `--force` — safe here
    because containers share the host GPU via the profile rather than
    pinning a passthrough device, so there is no FLR reset to corrupt.
    VMs are never force-stopped (qemu SIGKILL mid GPU-reset can wedge the
    vfio device in-kernel): a timed-out VM stop raises so the caller
    records an issue and retries next tick instead of blocking forever.
    """
    is_vm = (_instance_info(name) or {}).get("type") == "virtual-machine"
    deadline = _STOP_TIMEOUT_VM_S if is_vm else _STOP_TIMEOUT_CONTAINER_S
    try:
        return _incus("stop", name, timeout=deadline)
    except subprocess.TimeoutExpired:
        pass
    if is_vm:
        raise RuntimeError(
            f"incus stop {name} hung >{deadline}s; NOT forcing a VM "
            f"(GPU-reset wedge) — will retry next tick")
    force = _incus("stop", name, "--force", check=False,
                   timeout=_STOP_TIMEOUT_FORCE_S)
    if force.returncode == 0:
        return force
    raise RuntimeError(
        f"graceful incus stop {name} hung, then --force failed: "
        f"{force.stderr.strip() or 'still hung after force-stop timeout'}")

def _incus_json(*args):
    return json.loads(_incus(*args).stdout)

def _instance_exists(name):
    return _incus("info", name, check=False).returncode == 0

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
    """Set of IPs already in use on this host's incus bridge: every pinned
    eth0 address plus every currently-live address (e.g. the bastion's
    DHCP-assigned .36 that has no static pin)."""
    occupied = set(_pinned_ips().values())
    for inst in _incus_json("list", "--format", "json"):
        net = (inst.get("state") or {}).get("network") or {}
        for iface in net.values():
            for a in iface.get("addresses", []) or []:
                if a.get("family") == "inet" and a.get("scope") == "global":
                    occupied.add(a["address"])
    return occupied

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

def _ensure_nas_share(name):
    """Best-effort: provision /near1/vm/<name> on the host and attach it
    as a virtiofs disk at /data inside the instance. Idempotent — safe to
    call on every start as a backfill. Silently skips (warning only) if
    /near1 is unreachable, so a NAS outage doesn't block VM start.
    """
    # Per-host opt-out (`vm_nas_share: false` in HOST_CONFIG_PATH) for
    # hosts without a NAS mount — e.g. the dev container cluster, where
    # the virtiofs attach can't work anyway (no idmapping under nesting).
    if _host_config_value("vm_nas_share") == "false":
        return
    host_dir = f"{NAS_HOST_BASE}/{name}"
    # `timeout` guards a hung CIFS automount: if the NAS is down, an
    # access to /near1 can block forever on the systemd automount.
    try:
        subprocess.run(["mkdir", "-p", host_dir],
                       check=True, timeout=10,
                       stderr=subprocess.PIPE, text=True)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        msg = getattr(e, "stderr", "") or str(e)
        print(f"  NAS /near1 unreachable — skipping /data attach on "
              f"'{name}' ({msg.strip() or e.__class__.__name__}).",
              file=sys.stderr)
        return

    have = _incus("config", "device", "list", name, check=False)
    if NAS_DEVICE in have.stdout.split():
        return
    # On a VM host the share is served by virtiofsd, which maps uids in
    # userspace; shift=true is the established working path there. On a
    # container host the device is an idmapped bind mount, which the
    # CIFS-backed /near1 cannot provide ("idmapping abilities are
    # required but aren't supported on system") — so the container fails
    # to start. Containers instead align ids via the gpu-ct profile's
    # raw.idmap (uid/gid 1000 == the Samba user the CIFS mount pins), so
    # a plain bind mount is read/write without an idmapped mount.
    inst = _instance_info(name)
    is_vm = bool(inst) and inst.get("type") == "virtual-machine"
    shift = ["shift=true"] if is_vm else []
    _incus("config", "device", "add", name, NAS_DEVICE, "disk",
           f"source={host_dir}", f"path={NAS_GUEST_PATH}",
           *shift, "required=false")

def _instance_running(name):
    r = _incus("list", name, "--format", "csv", "-c", "ns", check=False)
    for line in r.stdout.splitlines():
        host_name, _, state = line.partition(",")
        if host_name == name:
            return state.upper() == "RUNNING"
    return False

def _wait_agent(name, timeout=180):
    """Wait until `incus exec` works inside an instance."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _incus("exec", name, "--", "true", check=False).returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError(f"'{name}' did not become reachable within {timeout}s")

def _push_file(inst, path, content, owner, mode="0600"):
    """Write `content` to `path` inside an instance, owned by `owner`."""
    directory = os.path.dirname(path)
    _incus("exec", inst, "--", "mkdir", "-p", directory)
    r = subprocess.run(
        ["incus", "file", "push", "-", f"{inst}{path}", "--mode", mode],
        input=content, text=True, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"incus file push to {inst}{path} failed: {r.stderr.strip()}")
    _incus("exec", inst, "--", "chown", "-R", f"{owner}:{owner}", directory)
    _incus("exec", inst, "--", "chmod", "700", directory)

def _read_file(inst, path):
    """Return the contents of `path` inside `inst`, or None if absent.

    Used by callers that want to compare against intended content before
    pushing, so that no-op re-syncs don't trigger spurious changes (or
    file-mtime churn that downstream watchers care about).
    """
    r = subprocess.run(
        ["incus", "file", "pull", f"{inst}{path}", "-"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout

# --------------------------------------------------------------------------
# GPU pool (discovered: GPUs bound to vfio-pci are available for passthrough)
# --------------------------------------------------------------------------
def _is_gpu_pci_class(cls):
    """True for a PCI display-controller base class (0x03xxxx).

    Covers the VGA class (0x0300xx) older passthrough cards expose and
    the 3D-controller class (0x0302xx) that newer Blackwell cards (e.g.
    RTX PRO 6000) expose as their only GPU function - both are single
    GPU functions bound to vfio-pci. Audio (.1) functions read 0x0403xx
    and stay excluded.
    """
    return cls.startswith("0x03")

def gpu_pool():
    """PCI addresses of GPU functions bound to vfio-pci."""
    drv = "/sys/bus/pci/drivers/vfio-pci"
    if not os.path.isdir(drv):
        return []
    gpus = []
    for entry in sorted(os.listdir(drv)):
        dev = f"/sys/bus/pci/devices/{entry}"
        if not os.path.isdir(dev):
            continue
        try:
            with open(f"{dev}/class") as f:
                cls = f.read().strip()
        except OSError:
            continue
        if _is_gpu_pci_class(cls):
            gpus.append(entry)
    return gpus

def _assigned_gpus():
    assigned = set()
    for inst in _incus_json("list", "--format", "json"):
        for dev in inst.get("devices", {}).values():
            if dev.get("type") == "gpu" and dev.get("pci"):
                assigned.add(dev["pci"])
    return assigned

def free_gpus():
    assigned = _assigned_gpus()
    return [g for g in gpu_pool() if g not in assigned]

def _is_live_vendor(config_bytes):
    """True if the first two PCI config-space bytes are a real vendor ID.
    A device whose link is down (DPC-contained) or otherwise inaccessible
    reads back 0x0000/0xffff."""
    if len(config_bytes) < 2:
        return False
    vid = config_bytes[0] | (config_bytes[1] << 8)
    return vid not in (0x0000, 0xffff)

def _gpu_healthy(pci):
    """Liveness probe for a free passthrough GPU.

    Resume the device to D0 (exactly what the guest firmware does on boot)
    and confirm a live config-space read returns its real vendor ID. A GPU
    wedged by a PCIe fault / DPC link-down — the c1:00.0 failure of
    2026-06-13 — stays D3hot-inaccessible and/or reads 0xffff; handed to a
    VM it makes OVMF spin for ~15 min enumerating dead BARs before the
    launch times out. Detecting it here lets us exclude it instead.

    Best-effort and side-effect-neutral: it restores the device's runtime
    PM policy, and any probe error counts as unhealthy. Only ever called
    on FREE GPUs — never poke a device a running guest owns.
    """
    dev = f"/sys/bus/pci/devices/{pci}"
    try:
        with open(f"{dev}/power/control", "w") as f:
            f.write("on")                  # pm_runtime_resume -> D0
        try:
            with open(f"{dev}/config", "rb") as f:
                vid = f.read(2)
        finally:
            with open(f"{dev}/power/control", "w") as f:
                f.write("auto")
    except OSError:
        return False
    return _is_live_vendor(vid)

def select_healthy_gpus(free, n, is_healthy):
    """Pick `n` GPUs from `free` (in pool order) that pass `is_healthy`.

    Pure (no I/O): the live callers pass free_gpus() and _gpu_healthy.
    Returns (picked, wedged) where `wedged` are the probed-unhealthy free
    GPUs (so the caller can log them). Raises ValueError — distinguishing
    "wedged" from merely "in use" — when fewer than `n` are healthy.
    """
    healthy, wedged = [], []
    for g in free:
        (healthy if is_healthy(g) else wedged).append(g)
    if len(healthy) < n:
        extra = (f"; {len(wedged)} free but wedged ({', '.join(wedged)})"
                 if wedged else "")
        raise ValueError(f"Only {len(healthy)} healthy GPU(s) free, "
                         f"requested {n}{extra}.")
    return healthy[:n], wedged

def _select_free_gpus(n):
    """Live wrapper around select_healthy_gpus: choose n healthy free GPUs
    and log any wedged ones skipped."""
    picked, wedged = select_healthy_gpus(free_gpus(), n, _gpu_healthy)
    for g in wedged:
        print(f"  GPU {g} excluded from allocation: failed liveness probe "
              f"(PCIe wedge / DPC link-down?)", file=sys.stderr)
    return picked

def _verify_guest_gpus(vm_name, expected):
    """Best-effort post-boot check: warn loudly if the guest doesn't see
    `expected` GPUs (a kernel staged without a matching NVIDIA module — the
    vm1 driver-loss pattern — or a card that fell off the bus). Never
    fails the operation; returns the count nvidia-smi reported."""
    r = _incus("exec", vm_name, "--", "nvidia-smi", "-L", check=False)
    seen = sum(1 for line in r.stdout.splitlines() if line.startswith("GPU "))
    if r.returncode != 0 or seen != expected:
        print(f"  WARNING: '{vm_name}' should see {expected} GPU(s) but "
              f"nvidia-smi reports {seen} (rc={r.returncode}). Check the "
              f"guest's NVIDIA driver / kernel headers.", file=sys.stderr)
    else:
        print(f"  verified {seen} GPU(s) visible in the guest.")
    return seen

def vm_gpus():
    pool = gpu_pool()
    if not pool:
        print("No vfio-pci GPUs found — has the host been rebooted since "
              "the passthrough binding was configured?")
        return
    assigned = _assigned_gpus()
    for g in pool:
        if g in assigned:
            status = "ASSIGNED"
        elif _gpu_healthy(g):
            status = "free"
        else:
            status = "WEDGED (PCIe fault? — won't be allocated)"
        print(f"{g}  {status}")

def _instance_info(name):
    """The instance's full JSON record, or None if it doesn't exist on
    this host."""
    insts = _incus_json("list", name, "--format", "json")
    for inst in insts:
        if inst.get("name") == name:
            return inst
    return None

def release_gpus(name):
    """Detach a stopped VM's passthrough GPU devices back to the host
    pool. The registry keeps the VM's GPU count — the next start
    re-acquires that many from the pool. Returns the number detached.

    Only meaningful for VMs (containers share the host GPU via the
    profile, there is nothing to release).
    """
    inst = _instance_info(name)
    if not inst or inst.get("type") != "virtual-machine":
        return 0
    if inst.get("status") == "Running":
        raise RuntimeError(f"'{name}' is running — stop it before "
                           f"releasing its GPUs")
    released = 0
    for dev_name, dev in sorted((inst.get("devices") or {}).items()):
        if isinstance(dev, dict) and dev.get("type") == "gpu" and dev.get("pci"):
            _incus("config", "device", "remove", name, dev_name)
            released += 1
    return released



# --------------------------------------------------------------------------
# user SSH key registry
# --------------------------------------------------------------------------
def _valid_vm_name(name):
    if not _VM_NAME_RE.match(name):
        raise ValueError(f"Invalid VM name '{name}': use lowercase letters, "
                         f"digits and hyphens (Incus instance name rules).")

def _valid_user(name):
    # Usernames are plain identifiers (often email addresses); they only
    # need to be safe as an etcd key component — no slashes or whitespace.
    if not _USER_RE.match(name):
        raise ValueError(f"Invalid user '{name}': allowed characters are "
                         f"letters, digits and . _ % + @ -")

def _host_config_value(wanted):
    """Read one key from HOST_CONFIG_PATH (written by install-incus.yml).
    Line-based on purpose — no PyYAML dependency. None when absent."""
    try:
        with open(HOST_CONFIG_PATH) as f:
            for line in f:
                key, _, val = line.partition(":")
                if key.strip() == wanted:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return None

def _default_instance_type():
    """Per-host default ('vm' or 'container') from HOST_CONFIG_PATH,
    written by admin/install-incus.yml. Fallback is 'vm' so legacy hosts
    (installed before the file existed) keep their prior behaviour."""
    v = _host_config_value("default_instance_type")
    return v if v in ("vm", "container") else "vm"

def user_get(user):
    return _get_json(USERS_PREFIX + user)

def users_all():
    return _all_json(USERS_PREFIX)

# Per-user key cap: bounds the etcd value and the bastion authorized_keys
# regenerated from it. Generous — real users have a handful.
MAX_SSH_KEYS_PER_USER = 25
# A real OpenSSH public-key line is well under this; the cap stops an
# oversized blob from being smuggled into etcd / authorized_keys.
MAX_SSH_KEY_LEN = 4096
# Shape checks only — we don't gatekeep the key algorithm, just that the
# tokens are well-formed (the type a plain identifier, the material base64).
_SSH_KEY_TYPE_RE = re.compile(r"^[A-Za-z0-9@._-]+$")
_SSH_KEY_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,3}$")

def validate_ssh_key(key):
    """Validate a single SSH public-key line; return the cleaned string.

    Security-critical: the value is later emitted verbatim into the jump
    bastion's authorized_keys (see _bastion_authorized_keys), so a stray
    newline or control char could split the line and smuggle in an entry
    WITHOUT the restrict/port-forwarding options. We check shape/characters,
    not the algorithm: a single '<type> <base64> [comment]' line of
    printable text, type a plain token, material valid base64."""
    if not isinstance(key, str):
        raise ValueError("That does not look like an SSH public key.")
    key = key.strip()
    if not key:
        raise ValueError("No SSH key provided.")
    if len(key) > MAX_SSH_KEY_LEN:
        raise ValueError("SSH key is too long.")
    # \n \r \t (and other C0 controls) and DEL: an embedded newline is the
    # injection vector; a single line of printable text is all that's valid.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in key):
        raise ValueError("An SSH key must be a single line of printable text.")
    parts = key.split(None, 2)             # type, base64, [comment]
    if len(parts) < 2:
        raise ValueError("That does not look like an SSH public key.")
    if not _SSH_KEY_TYPE_RE.match(parts[0]):
        raise ValueError("That does not look like an SSH public key.")
    if len(parts[1]) < 16 or not _SSH_KEY_B64_RE.match(parts[1]):
        raise ValueError("That does not look like an SSH public key.")
    return key

def user_add_key(user, key):
    _valid_user(user)
    key = validate_ssh_key(key)
    rec = user_get(user) or {"ssh_keys": []}
    if key in rec["ssh_keys"]:
        print(f"Key already registered for '{user}'.")
        return
    if len(rec["ssh_keys"]) >= MAX_SSH_KEYS_PER_USER:
        raise ValueError(
            f"Too many SSH keys ({MAX_SSH_KEYS_PER_USER} max) — remove one first.")
    rec["ssh_keys"].append(key)
    _put_json(USERS_PREFIX + user, rec)
    print(f"Added SSH key for '{user}' ({len(rec['ssh_keys'])} total).")

def user_remove_key(user, match):
    rec = user_get(user)
    if not rec:
        raise ValueError(f"No such user: {user}")
    kept = [k for k in rec.get("ssh_keys", []) if match not in k]
    removed = len(rec.get("ssh_keys", [])) - len(kept)
    if kept:
        rec["ssh_keys"] = kept
        _put_json(USERS_PREFIX + user, rec)
    else:
        _delete(USERS_PREFIX + user)          # no keys left -> drop the user
    print(f"Removed {removed} key(s) from '{user}'.")

def user_list(user=None):
    if user:
        rec = user_get(user)
        if not rec:
            print(f"No such user: {user}")
            return
        print(f"{user}:")
        for k in rec.get("ssh_keys", []):
            print(f"  {k}")
    else:
        users = users_all()
        if not users:
            print("No users registered.")
            return
        for name, rec in sorted(users.items()):
            print(f"{name}: {len(rec.get('ssh_keys', []))} key(s)")

# --------------------------------------------------------------------------
# VM registry + lifecycle
# --------------------------------------------------------------------------
def vm_get(name):
    return _get_json(VMS_PREFIX + name)

def vms_all():
    return _all_json(VMS_PREFIX)

def _vm_access_users(rec):
    """Users who may SSH to a VM: its owner plus any additional users.

    The owner stays the billing/accounting identity; additional_users are
    extra registered users granted the same SSH access. Returns a list
    without duplicates."""
    users = []
    owner = rec.get("owner")
    if owner:
        users.append(owner)
    for u in rec.get("additional_users") or []:
        if u and u not in users:
            users.append(u)
    return users

def _access_vms():
    """Return {user: [vm names]} for every user who can SSH to a VM.

    A user appears for each VM they own AND each VM they are an additional
    user on; a VM can map to several users."""
    out = {}
    for name, rec in vms_all().items():
        for u in _vm_access_users(rec):
            out.setdefault(u, []).append(name)
    return out

def _vm_access_keys(rec):
    """Union of every registered SSH key of all users with VM access.

    The VM's primary login user accepts the owner's keys *and* each
    additional user's keys, deduped by key material."""
    seen, keys = set(), []
    for u in _vm_access_users(rec):
        for k in (user_get(u) or {}).get("ssh_keys", []):
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys

def _guest_login_user(vm_name):
    """The VM's primary login user (uid 1000) — 'ubuntu' on cloud images."""
    r = _incus("exec", vm_name, "--", "getent", "passwd", "1000", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.split(":", 1)[0]
    return VM_GUEST_USER

# A GPU VM's OVMF firmware spends several minutes enumerating the large
# BARs of passed-through GPUs before the guest kernel even starts, so the
# agent takes far longer to appear than for a CPU-only VM.
GPU_VM_AGENT_TIMEOUT = 900

def _ensure_sshd(vm_name, agent_timeout=180):
    """Make sure the VM has an SSH server listening on port 22."""
    _wait_agent(vm_name, agent_timeout)
    # cloud-init creates the default user on first boot — wait for it.
    _incus("exec", vm_name, "--", "cloud-init", "status", "--wait", check=False)
    if _incus("exec", vm_name, "--", "test", "-x", "/usr/sbin/sshd",
              check=False).returncode != 0:
        print("Installing openssh-server in the VM...")
        _incus("exec", vm_name, "--", "bash", "-c",
               "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
               "apt-get install -y -qq openssh-server")
    # Use the plain service so :22 is reliably listening (Ubuntu 24.04
    # ships ssh socket-activated).
    _incus("exec", vm_name, "--", "bash", "-c",
           "systemctl disable --now ssh.socket 2>/dev/null || true; "
           "systemctl enable --now ssh.service")

def _inject_vm_keys(vm_name, keys, agent_timeout=180):
    """Install the given keys into the VM's primary login user."""
    _wait_agent(vm_name, agent_timeout)
    # cloud-init creates the default user on first boot — wait for it.
    _incus("exec", vm_name, "--", "cloud-init", "status", "--wait", check=False)
    guest = _guest_login_user(vm_name)
    content = "".join(k.strip() + "\n" for k in keys)
    _push_file(vm_name, f"/home/{guest}/.ssh/authorized_keys", content, guest)

def _sync_vm_keys(vm_name, agent_timeout=180):
    """Recompute and inject the union of all access-user keys for a VM.

    Only acts on a VM that exists and is running on this host; stopped or
    absent instances are skipped (they'd block _wait_agent for minutes)."""
    if not _instance_exists(vm_name):
        return False
    if not _instance_running(vm_name):
        print(f"Skipping '{vm_name}' (not running).")
        return False
    rec = vm_get(vm_name) or {}
    print(f"Refreshing keys in '{vm_name}'...")
    _inject_vm_keys(vm_name, _vm_access_keys(rec), agent_timeout)
    return True

def desired_get(name):
    return _get_json(VM_DESIRED_PREFIX + name)

def desired_set(name, desired):
    _put_json(VM_DESIRED_PREFIX + name, desired)

def desired_delete(name):
    _delete(VM_DESIRED_PREFIX + name)

def desired_all():
    """{name: desired record} for every managed VM."""
    return _all_json(VM_DESIRED_PREFIX)

def gpu_block_get(block_id):
    return _get_json(GPU_BLOCKS_PREFIX + block_id)

def gpu_block_set(block_id, rec):
    _put_json(GPU_BLOCKS_PREFIX + block_id, rec)

def gpu_block_delete(block_id):
    _delete(GPU_BLOCKS_PREFIX + block_id)

def gpu_blocks_all():
    """{block_id: record} for every admin GPU reservation."""
    return _all_json(GPU_BLOCKS_PREFIX)

# Stop-grace markers ({"warned_at": iso, "immediate"?: bool}).

def grace_set(name, rec):
    _put_json(VM_GRACE_PREFIX + name, rec)

def grace_delete(name):
    _delete(VM_GRACE_PREFIX + name)

def grace_all():
    return _all_json(VM_GRACE_PREFIX)

# Reconciler convergence failures ({"op", "error", "at"}), surfaced on the
# schedule page.
def issues_all():
    return _all_json(VM_ISSUE_PREFIX)

# Per-host incus state snapshots (status + gpu_pool), written by the sampler.
def state_get(host):
    return _get_json(VM_STATE_PREFIX + host)

def state_all():
    return _all_json(VM_STATE_PREFIX)

def _parse_window(w, now=None):
    """Parse one {start, end} window into a (start, end) pair of UTC
    datetimes, or None if malformed/naive/elapsed. `now` (if given)
    drops windows whose end is already past."""
    try:
        start = datetime.fromisoformat(str(w["start"]))
        end = datetime.fromisoformat(str(w["end"]))
    except (KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start or (now is not None and end <= now):
        return None
    return start, end

def desired_on(desired, now):
    """Evaluate a desired-state record at `now` (UTC).

    Windows are only consulted under 'schedule'; on/off/unmanaged keep
    their windows as dormant data (preserved across mode flips by the web
    page), so they must never be read as on/off intent. Anything that
    isn't 'on' or an active 'schedule' window is off."""
    mode = desired.get("mode")
    if mode == "on":
        return True
    if mode == "schedule":
        for w in desired.get("windows", []):
            parsed = _parse_window(w)
            if parsed and parsed[0] <= now < parsed[1]:
                return True
    return False

def pending_state(desired, status, grace, now):
    """What the reconciler will do to this VM on its next tick — 'start',
    'stop', or None (converged / unmanaged) — from its desired record vs
    its live `status`. Only managed modes converge. A `grace` marker is the
    reconciler's own (fresher) evidence of a running, pending-stop VM, used
    when the periodically-sampled `status` lags. The schedule page surfaces
    this as the ⏳ pending indicator."""
    if not desired or desired.get("mode") not in ("on", "off", "schedule"):
        return None
    want_on = desired_on(desired, now)
    live_on = status == "Running"
    if want_on and not live_on:
        return "start"
    if not want_on and (live_on or grace):
        return "stop"
    return None

# --------------------------------------------------------------------------
# GPU capacity commitments (admission control for the scheduling page)
#
# The dual of desired_on: rather than "is this VM on now", it answers
# "which GPUs are spoken for, when". A VM's registered GPUs are committed
# *always* under unmanaged (devices never released) and `on`, *never*
# under `off` (scheduled stop releases them), and during its windows
# under `schedule`. Containers share the host GPU and never enter pool
# accounting. Pure functions — no etcd, so unit-testable in isolation;
# the admission orchestration (etcd reads) stays in the admin app.
# --------------------------------------------------------------------------
def desired_gpus(desired, registered):
    """How many GPUs a VM should run with under a desired-state record.

    Uses the record's `gpus` when explicitly set (0 means run with no
    GPUs); falls back to the VM's registered allocation when absent
    (backwards compat — schedules written before this field meant 'all
    allocated GPUs')."""
    d = desired or {}
    if "gpus" in d:
        g = d["gpus"]
        if isinstance(g, int) and not isinstance(g, bool) \
                and 0 <= g <= registered:
            return g
    return registered

def gpu_commitments(registry, desired_all, now):
    """{host: [{vm, owner, gpus, mode, windows}]} implied by the registry
    and the accepted desired records. `windows` is None for always-held
    commitments, else a list of (start, end) UTC datetime pairs with
    elapsed windows dropped (a schedule with no live windows commits
    nothing and is omitted)."""
    hosts = {}
    for vm, rec in registry.items():
        if rec.get("type") == "container":
            continue
        host = rec.get("host")
        gpus = desired_gpus(desired_all.get(vm), rec.get("gpus") or 0)
        if not gpus or not host:
            continue
        mode = (desired_all.get(vm) or {}).get("mode", "unmanaged")
        if mode == "off":
            continue
        windows = None
        if mode == "schedule":
            windows = [p for p in
                       (_parse_window(w, now)
                        for w in (desired_all.get(vm) or {}).get("windows", []))
                       if p]
            if not windows:
                continue
        hosts.setdefault(host, []).append(
            {"vm": vm, "owner": rec.get("owner"), "gpus": gpus,
             "mode": mode, "windows": windows})
    return hosts

def gpu_block_commitments(blocks, now):
    """{host: [{vm, owner, gpus, mode, windows, is_block}]} implied by the
    admin GPU reservations `blocks` ({block_id: record}). Same shape as
    `gpu_commitments` (so `gpu_conflict` and the schedule page can treat
    blocks as ordinary held capacity) with `owner` always None, `vm` set
    to the block id, and `is_block` True to let the UI label them. A
    block's `mode` is either 'on' (held continuously) or 'schedule'
    (held during its live windows); an 'on'-mode block with no pool is
    merely dormant and holds nothing."""
    hosts = {}
    for block_id, b in blocks.items():
        gpus, host = b.get("gpus") or 0, b.get("host")
        if not gpus or not host:
            continue
        mode = b.get("mode", "on")
        windows = None
        if mode == "schedule":
            windows = [p for p in
                       (_parse_window(w, now) for w in b.get("windows", []))
                       if p]
            if not windows:
                continue
        hosts.setdefault(host, []).append(
            {"vm": block_id, "owner": None, "gpus": gpus, "mode": mode,
             "windows": windows, "is_block": True})
    return hosts

def combine_gpu_commitments(*commit_sets):
    """Merge any number of {host: [commit, ...]} dicts (from
    `gpu_commitments` / `gpu_block_commitments`) into one, preserving
    per-host order. Used so VM and block commitments are checked and shown
    together against a single pool."""
    merged = {}
    for cs in commit_sets:
        for host, items in cs.items():
            merged.setdefault(host, []).extend(items)
    return merged

def gpu_conflict(commits, gpus, pool, lo, hi):
    """Worst capacity violation for a candidate needing `gpus` GPUs
    throughout [lo, hi): (when, free_then, holders), or None if it fits.
    Committed capacity only changes at window starts, so probing `lo`
    plus every window start inside the range covers the peaks."""
    points = [lo]
    for c in commits:
        for s, _ in (c["windows"] or []):
            if lo < s < hi:
                points.append(s)
    worst = None
    for p in points:
        active = [c for c in commits
                  if c["windows"] is None
                  or any(s <= p < e for s, e in c["windows"])]
        free = pool - sum(c["gpus"] for c in active)
        if free < gpus and (worst is None or free < worst[1]):
            worst = (p, free, active)
    return worst

def gpu_available_windows(commits, gpus, pool, now, count=3, max_hours=6,
                          horizon_days=7, avoid_windows=None):
    """Future hourly-aligned windows where `gpus` GPUs fit `pool` against
    `commits`, for the schedule page's availability suggestions.

    Returns up to `count` windows, each starting at the next whole hour and
    lasting the longest continuous span that fits, capped at `max_hours`
    (6 by default, to discourage over-allocation). Suggested starts that
    overlap `avoid_windows` (a list of (start, end) UTC pairs) are skipped,
    so a VM's own already-scheduled windows don't get re-suggested.
    """
    if not isinstance(pool, int) or not gpus or gpus > pool:
        return []
    avoid_windows = avoid_windows or []
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    horizon = start + timedelta(days=horizon_days)
    out = []
    t = start
    while t < horizon and len(out) < count:
        end = None
        for hours in range(1, max_hours + 1):
            candidate = t + timedelta(hours=hours)
            if gpu_conflict(commits, gpus, pool, t, candidate) is None:
                end = candidate
        if end is not None and any(t < hi and lo < end
                                   for lo, hi in avoid_windows):
            t += timedelta(hours=1)
            continue
        if end is None:
            t += timedelta(hours=1)
            continue
        out.append({
            'start': t.isoformat(timespec='minutes'),
            'end': end.isoformat(timespec='minutes'),
            'hours': int((end - t).total_seconds() // 3600),
        })
        t = end
    return out

def _record_issue(issues, name, op, error, now):
    _put_json(VM_ISSUE_PREFIX + name,
              {"op": op, "error": str(error)[:300],
               "at": now.isoformat(timespec="seconds")})
    issues[name] = True

def _clear_issue(issues, name):
    if name in issues:
        _delete(VM_ISSUE_PREFIX + name)
        del issues[name]

def _reconcile_want_on(name, desired, running, grace, issues, to_start,
                       all_vms):
    """Handle a VM whose desired state is ON."""
    if grace:
        grace_delete(name)
        if running:
            print(f"reconcile: '{name}' desired flipped back on — "
                  f"cancelled pending stop")
    if running:
        _clear_issue(issues, name)
    else:
        # Deferred to the start pass; schedule the GPU count the
        # desired record calls for (explicit 0 = no GPUs, absent
        # = falling back to registered).
        to_start.append(
            (name, desired_gpus(desired,
                                all_vms.get(name, {}).get("gpus") or 0)))


def _reconcile_stop(name, grace, now, issues):
    """Stop a VM whose grace period has elapsed (or was bypassed)."""
    try:
        warned = datetime.fromisoformat(grace["warned_at"])
    except (KeyError, TypeError, ValueError):
        warned = None
    # `immediate` is set by the schedule page's "stop now" button to
    # bypass the remaining grace (the guest was already warned).
    immediate = bool(grace.get("immediate"))
    if immediate or warned is None or \
            (now - warned).total_seconds() >= SCHEDULED_STOP_GRACE_S:
        print(f"reconcile: stopping '{name}' (schedule/desired off, "
              f"{'immediate' if immediate else 'grace elapsed'})")
        try:
            vm_stop(name, initiator="scheduler")
            grace_delete(name)
            released = release_gpus(name)
            if released:
                print(f"  released {released} GPU(s) to the host pool")
            _clear_issue(issues, name)
        except Exception as e:
            print(f"  stop failed (will retry next tick; "
                  f"NOT forcing): {e}", file=sys.stderr)
            _record_issue(issues, name, "stop", e, now)


def _reconcile_want_off_running(name, grace, now, issues):
    """Handle a running VM whose desired state is OFF."""
    if not grace:
        print(f"reconcile: warning '{name}' of scheduled stop "
              f"(>= {SCHEDULED_STOP_GRACE_S}s grace)")
        _incus("exec", name, "--", "sh", "-c",
               "echo 'YCluster: scheduled shutdown within minutes "
               "(save your work)' | wall", check=False)
        grace_set(name, {"warned_at": now.isoformat(timespec="seconds")})
        return
    _reconcile_stop(name, grace, now, issues)


def _reconcile_start_pass(to_start, issues, now):
    """Start pass — after every release, so freed GPUs are visible to
    _ensure_gpus_attached's free_gpus() lookup."""
    for name, gpus in to_start:
        print(f"reconcile: starting '{name}' (schedule/desired on)")
        try:
            vm_start(name, billable=True, initiator="scheduler", gpus=gpus)
            _clear_issue(issues, name)
        except Exception as e:
            print(f"  start failed (will retry next tick): {e}",
                  file=sys.stderr)
            _record_issue(issues, name, "start", e, now)


def reconcile():
    """Converge local instances toward their desired power state.

    Run by the vm-reconciler timer on incus hosts. Only registered
    instances on THIS host with a desired-state record are touched;
    everything else keeps the status quo. Scheduler starts are billable
    (the owner asked for this runtime). Stops are graceful and
    tick-based: first tick walls a warning into the guest and stamps a
    grace marker, a later tick (grace elapsed, intent re-checked by
    getting here again) cleanly stops — NEVER --force (GPU FLR wedge) —
    then releases the VM's passthrough GPUs back to the host pool
    (vm_start re-acquires). Failed convergence prints, records a
    per-VM issue (shown on the schedule page), and retries next tick.
    """
    host = socket.gethostname()
    now = datetime.now(timezone.utc)
    statuses = {i.get("name"): i.get("status")
                for i in _incus_json("list", "--format", "json")}
    graces = grace_all()
    desireds = desired_all()
    issues = issues_all()
    all_vms = vms_all()
    local_vms = {name for name, rec in all_vms.items()
                 if rec.get("host") == host}
    # Two passes so a same-tick GPU handoff is atomic: pass 1 does every
    # stop/release (and warns/no-ops), returning cards to the pool; pass 2
    # then starts, so a VM scheduled up as another goes down claims the
    # freed GPUs this tick instead of failing acquisition and retrying.
    to_start = []
    for name in sorted(local_vms):
        desired = desireds.get(name)
        # No record, or an explicit 'unmanaged' record (which the web page
        # now stores to preserve a dormant schedule) — the scheduler never
        # touches it; keep the status quo.
        if not desired or desired.get("mode") == "unmanaged":
            _clear_issue(issues, name)
            continue
        want_on = desired_on(desired, now)
        running = statuses.get(name) == "Running"
        grace = graces.get(name)

        if want_on:
            _reconcile_want_on(name, desired, running, grace, issues,
                               to_start, all_vms)
        elif running:
            _reconcile_want_off_running(name, grace, now, issues)
        else:
            _clear_issue(issues, name)
            if grace:
                # Already stopped (e.g. the owner shut it down during
                # grace) — release as a scheduled stop would have.
                grace_delete(name)
                released = release_gpus(name)
                if released:
                    print(f"reconcile: '{name}' already stopped — released "
                          f"{released} GPU(s) to the host pool")

    _reconcile_start_pass(to_start, issues, now)

    # Issues for VMs that left the registry entirely would dangle
    # forever (vm_destroy clears its own, but a record can also vanish
    # by hand). Issues for other hosts' VMs are theirs to manage.
    for name in sorted(set(issues) - set(all_vms)):
        _delete(VM_ISSUE_PREFIX + name)


def _record_event(event, name, owner=None, gpus=None, initiator="cli",
                  billable=False):
    """Append a lifecycle event to the etcd queue (usage accounting).

    Best-effort: accounting must never break a lifecycle operation. CLI
    operations default to non-billable (admin debugging must not count
    against the owner's quota) — `--bill` opts in; the future
    scheduler/web path emits its own initiator with billable=True.
    """
    try:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vm": name,
            "host": socket.gethostname(),
            "event": event,
            "owner": owner,
            "gpus": gpus,
            "initiator": initiator,
            "billable": billable,
        }
        _put_json(f"{EVENTS_PREFIX}{time.time_ns()}-{name}", payload)
    except Exception as e:
        print(f"Warning: failed to record {event} event for {name}: {e}",
              file=sys.stderr)

def _record_event_from_record(event, name, initiator="cli", billable=False):
    rec = vm_get(name) or {}
    _record_event(event, name, owner=rec.get("owner"), gpus=rec.get("gpus"),
                  initiator=initiator, billable=billable)

def sample_state():
    """Snapshot local incus state into etcd for usage accounting.

    Run by the vm-state-sampler timer on incus hosts. One overwritten key
    per host; the collector on the storage leader turns fresh snapshots
    into usage_stats.vm_samples rows (stale snapshots — host down, timer
    dead — are skipped there, so absence is never mistaken for runtime).
    """
    instances = []
    for inst in _incus_json("list", "--format", "json"):
        name = inst.get("name")
        # The bastion is infrastructure, not a user instance.
        if not name or name == BASTION_CONTAINER:
            continue
        devices = inst.get("expanded_devices") or {}
        gpus = sum(1 for d in devices.values()
                   if isinstance(d, dict) and d.get("type") == "gpu")
        instances.append({"name": name, "status": inst.get("status"),
                          "type": inst.get("type"), "gpus": gpus})
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval_s": VM_STATE_INTERVAL_S,
        # Passthrough pool size — the schedule page's admission control
        # checks accepted windows against this capacity.
        "gpu_pool": len(gpu_pool()),
        "instances": instances,
    }
    _put_json(VM_STATE_PREFIX + socket.gethostname(), payload)
    print(f"sampled {len(instances)} instance(s)")

def vm_sync_limits():
    """Backfill cpu/mem from incus into VMS_PREFIX for VMs on this host.

    Runs locally on each compute host (no SSH needed — `_incus` is local).
    Only touches VMs already registered in VMS_PREFIX with `host == self`;
    updates the record only when cpu/mem are missing, so re-runs are no-ops
    once everything is backfilled. Use to migrate VMs launched before
    vm_launch started persisting limits (the Resources page needs them)."""
    me = socket.gethostname()
    vms = vms_all()
    updated = 0
    for name, rec in vms.items():
        if rec.get("host") != me:
            continue
        if rec.get("cpu") and rec.get("mem"):
            continue
        r = _incus("config", "get", name, "limits.cpu", check=False)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        cpu = r.stdout.strip()
        r = _incus("config", "get", name, "limits.memory", check=False)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        mem = r.stdout.strip()
        rec["cpu"] = cpu
        rec["mem"] = mem
        _put_json(VMS_PREFIX + name, rec)
        updated += 1
    print(f"synced cpu/mem for {updated} VM(s) on {me}")

def _validate_vm_launch(name, owner, gpus, instance_type):
    """Reject invalid names, GPU counts, types, and users without SSH keys."""
    _valid_vm_name(name)
    if gpus < 0:
        raise ValueError("--gpus cannot be negative")
    if instance_type in (None, "auto"):
        instance_type = _default_instance_type()
    if instance_type not in ("vm", "container"):
        raise ValueError(f"Invalid --type '{instance_type}' (vm|container|auto)")
    user = user_get(owner)
    if not user or not user.get("ssh_keys"):
        raise ValueError(
            f"User '{owner}' has no SSH keys registered. Add one first:\n"
            f"  ycluster vm ssh add {owner} '<public-key>'")
    if vm_get(name) or _instance_exists(name):
        raise ValueError(f"'{name}' already exists.")
    return instance_type


def _create_incus_instance(name, instance_type, image, profile, cpu, mem,
                           gpus, owner):
    """Create the instance via incus init, pin a static IP, sync DNS."""
    gpu_desc = f"{gpus} GPU" if gpus else "no GPU"
    print(f"Creating {instance_type} '{name}' ({cpu} CPU, {mem} RAM, "
          f"{gpu_desc}) owned by '{owner}'...")
    type_args = ["--vm"] if instance_type == "vm" else []
    _incus("init", image, name, *type_args, "--profile", profile,
           "-c", f"limits.cpu={cpu}", "-c", f"limits.memory={mem}")
    pinned_ip = _pick_vm_ip()
    _pin_instance_ip(name, pinned_ip)
    sync_dns_records()


def _attach_launch_gpus(name, instance_type, gpus, picked):
    """Attach GPU devices after instance creation (VM path) or strip the
    shared gpu0 device for a container launched with --gpus 0."""
    if instance_type == "vm":
        for i, pci in enumerate(picked):
            # A 'gpu' device of gputype=physical attaches the GPU's whole IOMMU
            # group (the HD-Audio .1 function comes with it) — do not add it
            # separately or Incus fails with "device is already attached".
            _incus("config", "device", "add", name, f"gpu{i}", "gpu",
                   "gputype=physical", f"pci={pci}")
    elif gpus == 0:
        # gpu-ct profile bakes in a shared gpu0 device. Strip it on --gpus 0.
        _incus("config", "device", "remove", name, "gpu0", check=False)


def _build_vm_record(name, owner, gpus, cpu, mem, instance_type):
    return {
        "owner": owner,
        "gpus": gpus,
        "cpu": cpu,
        "mem": mem,
        "type": instance_type,
        "host": socket.gethostname(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": "provisioning",
    }


def _wait_and_provision(name, record, instance_type, gpus):
    """Wait for boot, inject keys, sync bastion, and verify guest GPUs."""
    # Containers boot in seconds (no firmware enum); only GPU VMs need the
    # extended timeout.
    timeout = (GPU_VM_AGENT_TIMEOUT
               if (instance_type == "vm" and gpus) else 180)
    if instance_type == "vm" and gpus:
        print("Waiting for the VM to boot (GPU VMs are slow — the firmware "
              "enumerates the GPU BARs first), provisioning SSH...")
    else:
        print(f"Waiting for the {instance_type} to boot, provisioning SSH...")
    _ensure_sshd(name, timeout)
    _inject_vm_keys(name, _vm_access_keys(record), timeout)
    bastion_sync()

    # Confirm the guest actually sees the GPUs we passed through (catches a
    # driverless guest / a card that didn't enumerate); warn, never fail.
    if instance_type == "vm" and gpus:
        _verify_guest_gpus(name, gpus)


def vm_launch(name, owner, gpus=1, cpu=8, mem="32GiB", image=None,
              instance_type=None, billable=False):
    """Launch a GPU instance (VM or container).

    instance_type='auto' (or None) picks based on the host's
    /etc/ycluster/host.yml — 'vm' on VM hosts (NVIDIA passthrough),
    'container' on container hosts (AMD shared GPU). Override with
    instance_type='vm' or 'container'.

    For containers the gpu-ct profile already attaches the host's single
    GPU; --gpus 0 removes it, any positive --gpus value just keeps the
    profile's device (there's no per-GPU pinning on a single-GPU host).
    """
    instance_type = _validate_vm_launch(name, owner, gpus, instance_type)
    if image is None:
        image = VM_IMAGE if instance_type == "vm" else CT_IMAGE
    profile = VM_PROFILE if instance_type == "vm" else CT_PROFILE

    # GPU selection: VM path pins specific passthrough GPUs from the host's
    # vfio pool; container path shares the host's single GPU via the profile,
    # so 'gpus' here is just a 0/non-zero toggle. Wedged GPUs (PCIe
    # fault / DPC link-down) are probed out so a launch fails fast here
    # instead of hanging OVMF for ~15 min on a dead card.
    picked = []
    if instance_type == "vm" and gpus:
        picked = _select_free_gpus(gpus)

    _create_incus_instance(name, instance_type, image, profile, cpu, mem,
                           gpus, owner)
    _attach_launch_gpus(name, instance_type, gpus, picked)
    _ensure_nas_share(name)
    _incus("start", name)

    # Register immediately, before SSH provisioning, so the instance is
    # accounted for even if this command crashes — but mark it 'provisioning'
    # until the launch fully completes, so an interrupted launch is
    # recognisable.
    record = _build_vm_record(name, owner, gpus, cpu, mem, instance_type)
    _put_json(VMS_PREFIX + name, record)
    _record_event("launch", name, owner=owner, gpus=gpus, billable=billable)

    _wait_and_provision(name, record, instance_type, gpus)

    record["state"] = "ready"
    _put_json(VMS_PREFIX + name, record)

def vm_resize(name, size):
    """Grow a VM's root disk to `size` (e.g. '160GiB').

    The root disk is inherited from the 'gpu-vm' profile; this creates a
    per-instance override. Incus only applies a new root-disk size when the
    VM starts, so a running VM is restarted; cloud-init then grows the
    partition and filesystem on boot. Incus only supports growing.
    """
    _valid_vm_name(name)
    if not _instance_exists(name):
        raise ValueError(f"No such VM: {name}")
    # 'override' creates an instance-local copy of the profile device; if the
    # device is already overridden it fails, so fall back to 'set'.
    ov = _incus("config", "device", "override", name, "root",
                f"size={size}", check=False)
    if ov.returncode != 0:
        _incus("config", "device", "set", name, "root", f"size={size}")
    print(f"Root disk of '{name}' set to {size}.")

    running = any(i.get("status") == "Running"
                  for i in _incus_json("list", name, "--format", "json"))
    if running:
        print("Restarting the VM so the new size takes effect...")
        _incus("restart", name)
        rec = vm_get(name)
        _wait_agent(name, GPU_VM_AGENT_TIMEOUT
                    if (rec or {}).get("gpus") else 180)
        _incus("exec", name, "--", "cloud-init", "status", "--wait",
               check=False)
        print("  Restarted; cloud-init grew the filesystem.")
    else:
        print("  VM is stopped — the filesystem will grow on next boot "
              "(cloud-init growpart).")


def desired_conflict(desired, action, now):
    """Reason a manual `action` ('start'|'stop'|'restart') will be reverted
    by the reconciler, or None.

    A CLI lifecycle op doesn't touch the desired record, so the reconciler
    keeps converging toward it: stopping a VM the schedule wants ON gets
    restarted next tick; starting one the schedule wants OFF gets stopped
    (after grace). 'restart' ends running, so it conflicts like 'start'.
    None for an unmanaged VM (no record, or an explicit 'unmanaged' record
    preserving a dormant schedule) or an action that agrees with the
    schedule. Pure — unit-testable; the I/O wrapper supplies the record."""
    if not desired or desired.get("mode") == "unmanaged":
        return None
    want_on = desired_on(desired, now)
    mode = desired.get("mode")
    if action == "stop" and want_on:
        return (f"scheduled ON (mode={mode}) — the reconciler will restart "
                f"it within ~2 min; set mode 'off' to keep it stopped")
    if action in ("start", "restart") and not want_on:
        return (f"scheduled OFF (mode={mode}) — the reconciler will stop it "
                f"again after grace; adjust the schedule to keep it running")
    return None

def _warn_desired_conflict(name, action):
    """Print a heads-up if a CLI lifecycle op contradicts the VM's schedule
    (the reconciler would undo it). Only meaningful for CLI-initiated ops."""
    reason = desired_conflict(desired_get(name), action,
                              datetime.now(timezone.utc))
    if reason:
        print(f"  NOTE: '{name}' is {reason}.", file=sys.stderr)

def vm_stop(name, billable=False, initiator="cli", release=False):
    _incus_stop(name)
    _record_event_from_record("stop", name, initiator=initiator,
                              billable=billable)
    print(f"Stopped '{name}'.")
    # Opt-in: hand the passthrough GPUs back to the host pool (the next
    # start re-acquires via _ensure_gpus_attached). Off by default so a
    # plain stop keeps the cards for an immediate restart.
    if release:
        released = release_gpus(name)
        print(f"Released {released} GPU(s) to the host pool."
              if released else "No passthrough GPUs to release.")
    if initiator == "cli":
        _warn_desired_conflict(name, "stop")

def vm_start(name, billable=False, initiator="cli", gpus=None):
    """Start a VM, converging its passthrough GPUs to `gpus` (default:
    the VM's registered count). `gpus` is capped at the registered count
    so a caller can never over-allocate past the VM's allocation; 0
    runs the VM without any passthrough GPUs."""
    # Backfill any host-side state added since this instance was created
    # (currently just the /data NAS share). Idempotent.
    _ensure_nas_share(name)
    # Re-acquire GPUs the scheduler released on a previous stop (or drop
    # extras when starting with fewer than registered).
    registered = (vm_get(name) or {}).get("gpus") or 0
    if gpus is not None:
        if not isinstance(gpus, int) or isinstance(gpus, bool) \
                or gpus < 0 or gpus > registered:
            cap = f"0..{registered}"
            raise ValueError(f"gpus must be an integer in {cap} "
                             f"for '{name}' (registered count {registered})")
        _ensure_gpus_attached(name, want=gpus)
    else:
        _ensure_gpus_attached(name)
    _incus("start", name)
    _record_event_from_record("start", name, initiator=initiator,
                              billable=billable)
    print(f"Started '{name}'.")
    if initiator == "cli":
        _warn_desired_conflict(name, "start")

def vm_restart(name, billable=False, initiator="cli"):
    if _instance_running(name):
        _incus_stop(name)
    _ensure_nas_share(name)
    _ensure_gpus_attached(name)
    _incus("start", name)
    # Continuity event: interval assembly treats a restart as uninterrupted
    # running time (the stop/start gap is seconds).
    _record_event_from_record("restart", name, initiator=initiator,
                              billable=billable)
    print(f"Restarted '{name}'.")
    if initiator == "cli":
        _warn_desired_conflict(name, "restart")

def vm_destroy(name):
    if _instance_exists(name):
        # Stop cleanly first. Deleting a running GPU VM with --force SIGKILLs
        # qemu mid GPU-reset, which can wedge the device in-kernel (only a
        # host reboot clears it). A clean stop resets the GPU properly.
        running = any(i.get("status") == "Running"
                      for i in _incus_json("list", name, "--format", "json"))
        if running:
            _incus_stop(name)
        _incus("delete", name)
    _record_event_from_record("destroy", name)
    sync_dns_records()
    _delete(VMS_PREFIX + name)
    desired_delete(name)
    grace_delete(name)
    _delete(VM_ISSUE_PREFIX + name)
    bastion_sync()
    print(f"Destroyed '{name}' and removed its registration.")

def _strip_tz(ts: str) -> str:
    """Drop the timezone suffix from an ISO 8601 timestamp for display."""
    if not ts:
        return ""
    # Handle Z suffix.
    if ts.endswith("Z"):
        return ts[:-1]
    # Handle ±HH:MM offset (6 chars at the end).
    if len(ts) > 6 and ts[-6] in ("+", "-") and ts[-3] == ":":
        return ts[:-6]
    return ts

def vm_list():
    vms = vms_all()
    if not vms:
        print("No VMs registered.")
        return
    local = socket.gethostname()
    # Live state from local incus (always authoritative for this host).
    try:
        live_states = {i["name"]: i.get("status", "?")
                       for i in _incus_json("list", "--format", "json")}
    except FileNotFoundError:
        live_states = {}
    # Sampled state from every host's etcd snapshot (vm-state-sampler timer).
    sampled = {}
    for host, snap in state_all().items():
        for inst in snap.get("instances", []):
            name = inst.get("name")
            if name:
                sampled[name] = inst.get("status", "?")
    print(f"{'NAME':<20} {'OWNER':<24} {'HOST':<8} {'GPUS':<5} "
          f"{'STATE':<14} CREATED")
    for name, rec in sorted(vms.items()):
        host = rec.get("host")
        if rec.get("state") == "provisioning":
            state = "provisioning"        # launch did not complete
        elif host == local:
            state = live_states.get(name, "(absent)")
        else:
            # Prefer the etcd snapshot over "(remote)" when available.
            state = sampled.get(name, "(remote)")
        print(f"{name:<20} {rec.get('owner', '?'):<24} {host or '?':<8} "
              f"{str(rec.get('gpus', '?')):<5} {state:<14} "
              f"{_strip_tz(rec.get('created', ''))}")

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


def vm_sync_keys(user):
    """Recompute the injected key set for every running VM `user` accesses.

    A VM's authorized_keys holds the union of the owner's and every
    additional user's keys, so a change to any one user's keys (add/remove,
    or the record disappearing) is propagated by recomputing the whole VM.
    Removing a user's keys drops them out of every VM they access, revoking
    that access.

    Only acts on VMs that exist *on this host* (incus is local-only) and
    are currently RUNNING — stopped VMs would block on _wait_agent for
    minutes and aren't reachable anyway.
    """
    for name in _access_vms().get(user, []):
        _sync_vm_keys(name)

# --------------------------------------------------------------------------
# bastion access list
# --------------------------------------------------------------------------
def bastion_public_port(host):
    """Public frp bastion port the VM host's bastion is exposed on.

    MUST stay in sync with the port math in
    app/templates/frps.toml.j2 (that template is what actually
    binds the port on the frps server; this only predicts it). Index =
    the numeric suffix of the host name. idx 1-3 keep historical contiguous
    ports (nvidia 2210+idx, compute 2213+idx); idx > 3 lands in a per-type
    disjoint block (nvidia 2220+idx, compute 2240+idx) so adding a host of
    one type never shifts another type's ports. Returns None if `host`
    isn't a recognised <type><index> VM-host name.
    """
    m = re.fullmatch(r'([a-z]+)(\d+)', host or '')
    if not m:
        return None
    is_nv = m.group(1) == 'nv'
    idx = int(m.group(2))
    if idx <= 3:
        return (2210 if is_nv else 2213) + idx
    return (2220 if is_nv else 2240) + idx

def _bastion_authorized_keys():
    """Render the jump user's authorized_keys from the etcd registries.

    Each key line is restricted to port-forwarding only, and `permitopen`
    is locked to the :22 of exactly the VMs that user can access (owns or
    is an additional user on).

    Deduped by key material: if the same public key is registered under
    multiple etcd users (same human, multiple identities), we emit one
    line whose `permitopen` is the union of all those users' VMs. Otherwise
    OpenSSH would only honour the first matching line's restrictions and
    the rest would be silently shadowed.
    """
    users = users_all()
    access_vms = _access_vms()
    # key material (everything except the trailing comment) -> dict with
    # the canonical key line we'll emit and the set of VMs it can reach.
    by_key = {}
    for user, rec in sorted(users.items()):
        vmnames = access_vms.get(user, [])
        if not vmnames:
            continue                           # no access to any VM
        for key in rec.get("ssh_keys", []):
            parts = key.split(None, 2)         # type, material, [comment]
            if len(parts) < 2:
                continue                       # malformed entry, skip
            material = f"{parts[0]} {parts[1]}"
            entry = by_key.setdefault(material, {"key": key, "vms": set()})
            entry["vms"].update(vmnames)
    lines = ["# Generated by 'ycluster vm bastion-sync' — do not edit."]
    for material in sorted(by_key):
        entry = by_key[material]
        permits = ",".join(f'permitopen="{v}:22"' for v in sorted(entry["vms"]))
        opts = f"restrict,port-forwarding,{permits}"
        lines.append(f"{opts} {entry['key']}")
    return "\n".join(lines) + "\n"

def bastion_sync():
    if not _instance_exists(BASTION_CONTAINER):
        print(f"Bastion container '{BASTION_CONTAINER}' not found — skipping "
              f"sync. Run admin/install-vm-bastion.yml first.", file=sys.stderr)
        return
    content = _bastion_authorized_keys()
    keys_path = f"/home/{BASTION_JUMP_USER}/.ssh/authorized_keys"
    existing = _read_file(BASTION_CONTAINER, keys_path)
    n = sum(1 for line in content.splitlines() if not line.startswith("#"))
    if existing == content:
        print(f"Bastion sync: no changes ({n} key line(s) already in place).")
        return
    _push_file(BASTION_CONTAINER, keys_path, content, BASTION_JUMP_USER)
    print(f"Bastion synced: {n} authorized key line(s).")

def _extract_lifecycle_instance(line):
    """Parse one incus monitor JSON line into (action, instance_name) for
    started/restarted non-bastion instances. Returns (None, None) when the
    line is irrelevant."""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    md = ev.get("metadata") or {}
    action = md.get("action")
    if action not in ("instance-started", "instance-restarted"):
        return None, None
    # source is "/1.0/instances/<name>"
    src = md.get("source") or ""
    parts = src.strip("/").split("/")
    if len(parts) < 3 or parts[1] != "instances":
        return None, None
    return action, parts[2]


def _queue_key_sync(name, cond, dirty_users, bastion_dirty):
    """Queue a key re-sync for every user who can access `name`'s VM."""
    rec = vm_get(name)
    users = _vm_access_users(rec or {})
    if not users:
        return
    print(f"bastion-watch: lifecycle event on '{name}' "
          f"({', '.join(users)}) — queueing key sync", flush=True)
    with cond:
        for u in users:
            dirty_users.add(u)
        bastion_dirty[0] = True
        cond.notify()


def _incus_lifecycle_thread(cond, dirty_users, bastion_dirty):
    """Watch incus lifecycle events; on instance-started/restarted, queue a
    key re-sync for every user who can access that VM (owner + additional).

    Covers the case where a VM that was stopped (or in ERROR) when the
    watcher booted gets started later — no etcd event fires, so this is
    the only way to notice. Runs in a daemon thread; if `incus monitor`
    dies, it is restarted with a short backoff.
    """
    while True:
        try:
            proc = subprocess.Popen(
                ["incus", "monitor", "--type=lifecycle", "--format=json"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                action, name = _extract_lifecycle_instance(line)
                if action is None:
                    continue
                if name == BASTION_CONTAINER:
                    continue
                _queue_key_sync(name, cond, dirty_users, bastion_dirty)
            proc.wait()
        except Exception as e:
            print(f"bastion-watch: incus monitor crashed: {e!r}",
                  file=sys.stderr, flush=True)
        time.sleep(5)                              # backoff before restart


def _watch_response_keys(response, prefix):
    """Return the changed keys (suffix past `prefix`) from an etcd3 watch
    callback response, or None if the response is a stream-break error.

    etcd3 0.12.x delivers a grpc.RpcError (which has no `.events`) to
    prefix-watch callbacks when the stream breaks, before clearing every
    callback without re-arming the watch. A None return signals a dead watch
    that the caller must recover by re-registering and doing a full resync.
    """
    events = getattr(response, "events", None)
    if events is None:
        return None
    keys = []
    for ev in events:
        key = ev.key.decode()
        if key.startswith(prefix):
            keys.append(key[len(prefix):])
    return keys

def _watcher_healthy(watch_ids, watcher):
    """True if every expected watch is still armed on an etcd3 Watcher.

    etcd3 clears `_callbacks` (and lets its daemon thread die) after a watch
    stream error; checking both lets the long-running bastion-watch recover
    instead of silently dropping future registry changes.
    """
    thread = getattr(watcher, "_callback_thread", None)
    if thread is None or not thread.is_alive():
        return False
    callbacks = getattr(watcher, "_callbacks", None) or {}
    return bool(watch_ids) and all(wid in callbacks for wid in watch_ids)

def _sync_vm_keys_quietly(name):
    """Sync one VM's keys, swallowing errors (logged to stderr)."""
    try:
        _sync_vm_keys(name)
    except Exception as e:
        print(f"bastion-watch: _sync_vm_keys({name}) failed: {e}",
              file=sys.stderr, flush=True)


def _sync_user_keys_quietly(user):
    """Sync one user's keys, swallowing errors (logged to stderr)."""
    try:
        vm_sync_keys(user)
    except Exception as e:
        print(f"bastion-watch: vm_sync_keys({user}) failed: {e}",
              file=sys.stderr, flush=True)


def _repair_and_resync(repair, full_resync, debounce):
    """Retry watch repair until it succeeds, then do a full key resync."""
    while True:
        try:
            repair()
            break
        except Exception as e:
            print(f"bastion-watch: watch repair failed: {e!r}; "
                  f"retrying in {debounce}s", file=sys.stderr,
                  flush=True)
            time.sleep(debounce)
    full_resync()


def _drain_dirty(cond, dirty_users, dirty_vms, bastion_dirty, watch_broken):
    """Clear and return the pending work sets under the condition lock."""
    with cond:
        users = set(dirty_users); dirty_users.clear()
        vms = set(dirty_vms); dirty_vms.clear()
        broken = watch_broken[0]
        watch_broken[0] = False
        had_work = bastion_dirty[0]
        bastion_dirty[0] = False
    return users, vms, broken, had_work


def _sync_dirty_vms(vms, users):
    """Re-sync keys for each changed VM and collect its access users."""
    for name in vms:
        rec = vm_get(name)
        for u in _vm_access_users(rec or {}):
            users.add(u)
        _sync_vm_keys_quietly(name)


def _sync_users(users):
    try:
        bastion_sync()
    except Exception as e:
        print(f"bastion-watch: bastion_sync failed: {e}",
              file=sys.stderr, flush=True)
    for user in sorted(users):
        _sync_user_keys_quietly(user)


def _bastion_watch_tick(cond, dirty_users, dirty_vms, bastion_dirty,
                        watch_broken, debounce, health_check_s,
                        _watch_ok, repair, full_resync):
    """One debounce tick of the bastion-watch loop.

    Waits for work, drains the dirty sets, handles watch-stream breaks (via
    repair + full_resync), then re-syncs the bastion and affected VM/user
    keys. Shared state is protected by `cond`.
    """
    with cond:
        while not bastion_dirty[0]:
            if not cond.wait(timeout=health_check_s):
                break                  # periodic liveness probe while idle
    time.sleep(debounce)               # let the burst settle

    users, vms, broken, had_work = _drain_dirty(
        cond, dirty_users, dirty_vms, bastion_dirty, watch_broken)
    if not _watch_ok():
        broken = True
    if broken:
        _repair_and_resync(repair, full_resync, debounce)
        return
    if not had_work:
        return                         # idle wake; watches are healthy

    _sync_dirty_vms(vms, users)
    _sync_users(users)


def bastion_watch(debounce=2.0):
    """Watch the user and VM registries; re-sync the bastion and VM keys
    on any change.

    Long-running. Initial sync runs first (covers events missed while we
    were down), then etcd watches on /cluster/users/ and /cluster/vms/
    drive subsequent syncs. A third watcher consumes the incus lifecycle
    event stream so VMs that come up later also get their keys pushed.
    Events are coalesced with a `debounce`-second settle window so a
    burst becomes one sync.

    Three kinds of trigger:
      * etcd /cluster/users/<u> change  → bastion_sync + vm_sync_keys(u)
      * etcd /cluster/vms/<v> change    → bastion_sync + vm_sync_keys(each
                                         user who can access <v>)
      * incus instance-started/restarted → vm_sync_keys(each user who
                                         can access <v>)

    vm_sync_keys acts only on RUNNING VMs that live on this host.

    The etcd3 watch stream can break underneath us (a grpc.RpcError): etcd3
    0.12.x then clears every callback without re-arming the watch, so the
    daemon would otherwise stay alive but silently miss *all* future registry
    changes. We detect that in the callback and via a periodic health check,
    then re-register both watches and do a full bastion/VM-key resync (the
    triggering key's identity is lost on a break, so we can't target one
    user).
    """
    import threading

    if not _instance_exists(BASTION_CONTAINER):
        print(f"Bastion container '{BASTION_CONTAINER}' not found — exiting.",
              file=sys.stderr)
        sys.exit(1)

    print("bastion-watch: initial sync...", flush=True)
    bastion_sync()
    # Initial VM-key sync for every user with access to VMs on this host.
    for user in sorted(_access_vms()):
        try:
            vm_sync_keys(user)
        except Exception as e:
            print(f"bastion-watch: initial vm_sync_keys({user}) failed: {e}",
                  file=sys.stderr, flush=True)
    print(f"bastion-watch: watching {USERS_PREFIX} and {VMS_PREFIX} "
          f"(debounce={debounce}s)", flush=True)

    HEALTH_CHECK_S = 30.0                 # periodic etcd-watch liveness probe

    cond = threading.Condition()
    dirty_users = set()                       # users whose records changed
    dirty_vms = set()                         # VM names whose records changed
    bastion_dirty = [False]
    watch_broken = [False]                    # etcd3 watch stream died

    def _on_event(response, prefix, dest_set):
        # etcd3 (0.12.x) passes a grpc.RpcError to the callback when the watch
        # stream breaks, then clears every callback *without* re-arming the
        # watch. A None return means the stream is gone and the triggering
        # key's identity is lost, so ask the main loop to recover.
        keys = _watch_response_keys(response, prefix)
        if keys is None:
            print(f"bastion-watch: watch on {prefix} broke ({response!r}) — "
                  f"re-registering and doing a full resync", file=sys.stderr,
                  flush=True)
            with cond:
                watch_broken[0] = True
                bastion_dirty[0] = True
                cond.notify()
            return
        with cond:
            bastion_dirty[0] = True
            dest_set.update(keys)
            print(f"bastion-watch: {len(keys)} event(s) on {prefix}",
                  flush=True)
            cond.notify()

    def on_user_event(response):
        _on_event(response, USERS_PREFIX, dirty_users)

    def on_vm_event(response):
        _on_event(response, VMS_PREFIX, dirty_vms)

    client = get_etcd_client()
    watch_ids = []

    def _register_watches():
        nonlocal client
        w1 = client.add_watch_prefix_callback(USERS_PREFIX, on_user_event)
        w2 = client.add_watch_prefix_callback(VMS_PREFIX, on_vm_event)
        watch_ids[:] = [w1, w2]
        print(f"bastion-watch: registered watches w1={w1} w2={w2}", flush=True)

    _register_watches()

    def _watch_ok():
        return _watcher_healthy(watch_ids, client.watcher)

    def _repair_watches():
        # A dead watcher thread can't be revived by re-adding callbacks (the
        # new requests would never be consumed) — swap in a fresh client.
        nonlocal client
        thread = getattr(client.watcher, "_callback_thread", None)
        if thread is not None and not thread.is_alive():
            print("bastion-watch: etcd watch thread died — "
                  "recreating client", file=sys.stderr, flush=True)
            reset_etcd_client()
            client = get_etcd_client()
            _register_watches()
            return
        try:
            _register_watches()
        except Exception as e:
            print(f"bastion-watch: re-register failed ({e!r}) — "
                  f"recreating client", file=sys.stderr, flush=True)
            reset_etcd_client()
            client = get_etcd_client()
            _register_watches()

    def _full_resync():
        """Recompute the bastion ACL and every VM's keys from scratch.

        Used after a watch-stream break: the failed event no longer carries
        the changed user, so we can't limit the resync to a single user.
        """
        try:
            bastion_sync()
        except Exception as e:
            print(f"bastion-watch: full_resync bastion_sync failed: {e}",
                  file=sys.stderr, flush=True)
        for user in sorted(_access_vms()):
            try:
                vm_sync_keys(user)
            except Exception as e:
                print(f"bastion-watch: full_resync vm_sync_keys({user}) "
                      f"failed: {e}", file=sys.stderr, flush=True)

    threading.Thread(
        target=_incus_lifecycle_thread,
        args=(cond, dirty_users, bastion_dirty),
        daemon=True,
    ).start()
    print("bastion-watch: incus lifecycle monitor started", flush=True)

    while True:
        _bastion_watch_tick(
            cond, dirty_users, dirty_vms, bastion_dirty, watch_broken,
            debounce, HEALTH_CHECK_S,
            _watch_ok=lambda: _watcher_healthy(watch_ids, client.watcher),
            repair=_repair_watches, full_resync=_full_resync,
        )


from .vm_alter import vm_alter  # noqa: E402  # moved to keep modules focused

from .gpu_attach import _ensure_gpus_attached  # noqa: E402
