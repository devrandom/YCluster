"""GPU VM hosting on Incus.

Manages GPU-passthrough VMs, a per-user SSH key registry, a VM-ownership
registry, and the SSH jump bastion's access list. Runs on the Incus host
(nv2): it shells out to the local `incus` client and reads/writes etcd.

etcd layout:
  /cluster/users/<user>   -> {"ssh_keys": [...]}
  /cluster/vms/<name>     -> {"owner": <user>, "gpus": N, "created": <iso>}
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..common.etcd_utils import get_etcd_client

USERS_PREFIX = "/cluster/users/"
VMS_PREFIX = "/cluster/vms/"

VM_PROFILE = "gpu-vm"
VM_IMAGE = "ubuntu-cuda-vllm"          # default for all VMs (GPU or not):
                                       # ubuntu-cuda + vLLM + FlashInfer AOT
                                       # cache. Pass --image ubuntu-cuda to
                                       # skip the vLLM layer (~1 GiB lighter).
VM_GUEST_USER = "ubuntu"               # default user in the Ubuntu cloud image
BASTION_CONTAINER = "bastion"
BASTION_JUMP_USER = "jump"

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
def _incus(*args, check=True, stdin=None):
    r = subprocess.run(
        ["incus", *args], input=stdin, text=True, capture_output=True
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"incus {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def _incus_json(*args):
    return json.loads(_incus(*args).stdout)


def _instance_exists(name):
    return _incus("info", name, check=False).returncode == 0


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


# --------------------------------------------------------------------------
# GPU pool (discovered: GPUs bound to vfio-pci are available for passthrough)
# --------------------------------------------------------------------------
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
        if cls.startswith("0x0300"):          # 0x0300xx = VGA controller
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


def vm_gpus():
    pool = gpu_pool()
    if not pool:
        print("No vfio-pci GPUs found — has the host been rebooted since "
              "the passthrough binding was configured?")
        return
    assigned = _assigned_gpus()
    for g in pool:
        print(f"{g}  {'ASSIGNED' if g in assigned else 'free'}")


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


def user_get(user):
    return _get_json(USERS_PREFIX + user)


def users_all():
    return _all_json(USERS_PREFIX)


def user_add_key(user, key):
    _valid_user(user)
    key = key.strip()
    if not re.match(r"^(ssh-|ecdsa-|sk-)", key):
        raise ValueError("That does not look like an SSH public key.")
    rec = user_get(user) or {"ssh_keys": []}
    if key in rec["ssh_keys"]:
        print(f"Key already registered for '{user}'.")
        return
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


def _owner_vms():
    """Return {owner: [vm names]}."""
    out = {}
    for name, rec in vms_all().items():
        out.setdefault(rec.get("owner"), []).append(name)
    return out


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


def _inject_owner_keys(vm_name, keys, agent_timeout=180):
    """Install an owner's keys into the VM's primary login user."""
    _wait_agent(vm_name, agent_timeout)
    # cloud-init creates the default user on first boot — wait for it.
    _incus("exec", vm_name, "--", "cloud-init", "status", "--wait", check=False)
    guest = _guest_login_user(vm_name)
    content = "".join(k.strip() + "\n" for k in keys)
    _push_file(vm_name, f"/home/{guest}/.ssh/authorized_keys", content, guest)


def vm_launch(name, owner, gpus=1, cpu=8, mem="32GiB", image=VM_IMAGE):
    _valid_vm_name(name)
    if gpus < 0:
        raise ValueError("--gpus cannot be negative")
    user = user_get(owner)
    if not user or not user.get("ssh_keys"):
        raise ValueError(
            f"User '{owner}' has no SSH keys registered. Add one first:\n"
            f"  ycluster vm ssh add {owner} '<public-key>'")
    if vm_get(name) or _instance_exists(name):
        raise ValueError(f"VM '{name}' already exists.")

    picked = []
    if gpus:
        free = free_gpus()
        if len(free) < gpus:
            raise ValueError(f"Only {len(free)} GPU(s) free, requested {gpus}.")
        picked = free[:gpus]

    gpu_desc = f"{gpus} GPU" if gpus else "no GPU"
    print(f"Creating VM '{name}' ({cpu} CPU, {mem} RAM, {gpu_desc}) "
          f"owned by '{owner}'...")
    _incus("init", image, name, "--vm", "--profile", VM_PROFILE,
           "-c", f"limits.cpu={cpu}", "-c", f"limits.memory={mem}")
    for i, pci in enumerate(picked):
        # A 'gpu' device of gputype=physical attaches the GPU's whole IOMMU
        # group (the HD-Audio .1 function comes with it) — do not add it
        # separately or Incus fails with "device is already attached".
        _incus("config", "device", "add", name, f"gpu{i}", "gpu",
               "gputype=physical", f"pci={pci}")
    _incus("start", name)

    # Register immediately, before SSH provisioning, so the VM is accounted
    # for even if this command crashes — but mark it 'provisioning' until
    # the launch fully completes, so an interrupted launch is recognisable.
    record = {
        "owner": owner,
        "gpus": gpus,
        "host": socket.gethostname(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": "provisioning",
    }
    _put_json(VMS_PREFIX + name, record)

    timeout = GPU_VM_AGENT_TIMEOUT if gpus else 180
    if gpus:
        print("Waiting for the VM to boot (GPU VMs are slow — the firmware "
              "enumerates the GPU BARs first), provisioning SSH...")
    else:
        print("Waiting for the VM to boot, provisioning SSH...")
    _ensure_sshd(name, timeout)
    _inject_owner_keys(name, user["ssh_keys"], timeout)
    bastion_sync()

    record["state"] = "ready"
    _put_json(VMS_PREFIX + name, record)

    where = f"GPU(s): {', '.join(picked)}" if picked else "no GPU"
    print(f"Launched '{name}' ({where})")
    print(f"  ssh -J {BASTION_JUMP_USER}@<rathole-host>:2210 "
          f"{VM_GUEST_USER}@{name}")


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


def vm_stop(name):
    _incus("stop", name)
    print(f"Stopped '{name}'.")


def vm_start(name):
    _incus("start", name)
    print(f"Started '{name}'.")


def vm_destroy(name):
    if _instance_exists(name):
        # Stop cleanly first. Deleting a running GPU VM with --force SIGKILLs
        # qemu mid GPU-reset, which can wedge the device in-kernel (only a
        # host reboot clears it). A clean stop resets the GPU properly.
        running = any(i.get("status") == "Running"
                      for i in _incus_json("list", name, "--format", "json"))
        if running:
            _incus("stop", name)
        _incus("delete", name)
    _delete(VMS_PREFIX + name)
    bastion_sync()
    print(f"Destroyed '{name}' and removed its registration.")


def vm_list():
    vms = vms_all()
    if not vms:
        print("No VMs registered.")
        return
    local = socket.gethostname()
    states = {i["name"]: i.get("status", "?")
              for i in _incus_json("list", "--format", "json")}
    print(f"{'NAME':<20} {'OWNER':<24} {'HOST':<8} {'GPUS':<5} "
          f"{'STATE':<14} CREATED")
    for name, rec in sorted(vms.items()):
        host = rec.get("host")
        if rec.get("state") == "provisioning":
            state = "provisioning"        # launch did not complete
        elif host is None or host == local:
            # Incus state is only visible for VMs on this host.
            state = states.get(name, "(absent)")
        else:
            state = "(remote)"
        print(f"{name:<20} {rec.get('owner', '?'):<24} {host or '?':<8} "
              f"{str(rec.get('gpus', '?')):<5} {state:<14} "
              f"{rec.get('created', '')}")


def vm_sync_keys(user):
    """Re-inject a user's current keys into every running VM they own.

    If the user has no keys left (or the record is gone), the VMs'
    authorized_keys are emptied — removing a user's keys revokes access.

    Only acts on VMs that exist *on this host* (incus is local-only) and
    are currently RUNNING — stopped VMs would block on _wait_agent for
    minutes and aren't reachable anyway.
    """
    rec = user_get(user) or {"ssh_keys": []}
    keys = rec.get("ssh_keys", [])
    for name in _owner_vms().get(user, []):
        if not _instance_exists(name):
            continue                           # VM not on this host
        if not _instance_running(name):
            print(f"Skipping '{name}' (not running).")
            continue
        print(f"Refreshing keys in '{name}'...")
        _inject_owner_keys(name, keys)


# --------------------------------------------------------------------------
# bastion access list
# --------------------------------------------------------------------------
def _bastion_authorized_keys():
    """Render the jump user's authorized_keys from the etcd registries.

    Each key line is restricted to port-forwarding only, and `permitopen`
    is locked to the :22 of exactly the VMs that user owns.

    Deduped by key material: if the same public key is registered under
    multiple etcd owners (same human, multiple identities), we emit one
    line whose `permitopen` is the union of all owners' VMs. Otherwise
    OpenSSH would only honour the first matching line's restrictions and
    the rest would be silently shadowed.
    """
    users = users_all()
    owner_vms = _owner_vms()
    # key material (everything except the trailing comment) -> dict with
    # the canonical key line we'll emit and the set of VMs it can reach.
    by_key = {}
    for owner, rec in sorted(users.items()):
        vmnames = owner_vms.get(owner, [])
        if not vmnames:
            continue                           # owns no VMs -> no jump access
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
    _push_file(BASTION_CONTAINER,
               f"/home/{BASTION_JUMP_USER}/.ssh/authorized_keys",
               content, BASTION_JUMP_USER)
    n = sum(1 for line in content.splitlines() if not line.startswith("#"))
    print(f"Bastion synced: {n} authorized key line(s).")


def _incus_lifecycle_thread(cond, dirty_users, bastion_dirty):
    """Watch incus lifecycle events; on instance-started/restarted, queue a
    key re-sync for that VM's owner.

    Covers the case where a VM that was stopped (or in ERROR) when the
    watcher booted gets started later — no etcd event fires, so this is
    the only way to notice. Runs in a daemon thread; if `incus monitor`
    dies, it is restarted with a short backoff.
    """
    relevant = ("instance-started", "instance-restarted")
    while True:
        try:
            proc = subprocess.Popen(
                ["incus", "monitor", "--type=lifecycle", "--format=json"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                md = ev.get("metadata") or {}
                action = md.get("action")
                if action not in relevant:
                    continue
                src = md.get("source") or ""
                # source is "/1.0/instances/<name>"
                parts = src.strip("/").split("/")
                if len(parts) < 3 or parts[1] != "instances":
                    continue
                name = parts[2]
                if name == BASTION_CONTAINER:
                    continue
                rec = vm_get(name)
                owner = rec.get("owner") if rec else None
                if not owner:
                    continue
                print(f"bastion-watch: lifecycle '{action}' on '{name}' "
                      f"(owner={owner}) — queueing key sync", flush=True)
                with cond:
                    dirty_users.add(owner)
                    bastion_dirty[0] = True
                    cond.notify()
            proc.wait()
        except Exception as e:
            print(f"bastion-watch: incus monitor crashed: {e!r}",
                  file=sys.stderr, flush=True)
        time.sleep(5)                              # backoff before restart


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
      * etcd /cluster/vms/<v> change    → bastion_sync + vm_sync_keys(<v owner>)
      * incus instance-started/restarted → vm_sync_keys(<v owner>)

    vm_sync_keys acts only on RUNNING VMs that live on this host.
    """
    import threading

    if not _instance_exists(BASTION_CONTAINER):
        print(f"Bastion container '{BASTION_CONTAINER}' not found — exiting.",
              file=sys.stderr)
        sys.exit(1)

    print("bastion-watch: initial sync...", flush=True)
    bastion_sync()
    # Initial VM-key sync for every owner with VMs on this host.
    for owner in sorted(_owner_vms()):
        try:
            vm_sync_keys(owner)
        except Exception as e:
            print(f"bastion-watch: initial vm_sync_keys({owner}) failed: {e}",
                  file=sys.stderr, flush=True)
    print(f"bastion-watch: watching {USERS_PREFIX} and {VMS_PREFIX} "
          f"(debounce={debounce}s)", flush=True)

    cond = threading.Condition()
    dirty_users = set()                       # users whose records changed
    dirty_vms = set()                         # VM names whose records changed
    bastion_dirty = [False]

    def _on_event(response, prefix, dest_set):
        try:
            n = len(response.events) if hasattr(response, "events") else 0
            print(f"bastion-watch: {n} event(s) on {prefix}", flush=True)
            with cond:
                bastion_dirty[0] = True
                for ev in (response.events or []):
                    key = ev.key.decode()
                    if key.startswith(prefix):
                        dest_set.add(key[len(prefix):])
                cond.notify()
        except Exception as e:
            print(f"bastion-watch: callback error on {prefix}: {e!r}",
                  file=sys.stderr, flush=True)
            with cond:
                bastion_dirty[0] = True
                cond.notify()

    def on_user_event(response):
        _on_event(response, USERS_PREFIX, dirty_users)

    def on_vm_event(response):
        _on_event(response, VMS_PREFIX, dirty_vms)

    client = get_etcd_client()
    w1 = client.add_watch_prefix_callback(USERS_PREFIX, on_user_event)
    w2 = client.add_watch_prefix_callback(VMS_PREFIX, on_vm_event)
    print(f"bastion-watch: registered watches w1={w1} w2={w2}", flush=True)

    threading.Thread(
        target=_incus_lifecycle_thread,
        args=(cond, dirty_users, bastion_dirty),
        daemon=True,
    ).start()
    print("bastion-watch: incus lifecycle monitor started", flush=True)

    while True:
        with cond:
            while not bastion_dirty[0]:
                cond.wait()
        time.sleep(debounce)                  # let the burst settle
        with cond:
            users = set(dirty_users); dirty_users.clear()
            vms = set(dirty_vms); dirty_vms.clear()
            bastion_dirty[0] = False
        # VM-record changes can shift ownership; pull each changed VM's
        # current owner into the per-user resync set.
        for name in vms:
            rec = vm_get(name)
            if rec and rec.get("owner"):
                users.add(rec["owner"])
        try:
            bastion_sync()
        except Exception as e:
            print(f"bastion-watch: bastion_sync failed: {e}",
                  file=sys.stderr, flush=True)
        for user in sorted(users):
            try:
                vm_sync_keys(user)
            except Exception as e:
                print(f"bastion-watch: vm_sync_keys({user}) failed: {e}",
                      file=sys.stderr, flush=True)
