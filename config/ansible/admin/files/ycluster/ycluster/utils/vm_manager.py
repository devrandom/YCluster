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
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..common.etcd_utils import get_etcd_client

USERS_PREFIX = "/cluster/users/"
VMS_PREFIX = "/cluster/vms/"

GPU_VM_PROFILE = "gpu-vm"
GPU_VM_IMAGE = "ubuntu-cuda"
GPU_VM_GUEST_USER = "ubuntu"          # default user in the Ubuntu cloud image
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


def _inject_owner_keys(vm_name, keys):
    """Install an owner's keys into a VM's guest user."""
    _wait_agent(vm_name)
    content = "".join(k.strip() + "\n" for k in keys)
    _push_file(vm_name, f"/home/{GPU_VM_GUEST_USER}/.ssh/authorized_keys",
               content, GPU_VM_GUEST_USER)


def vm_launch(name, owner, gpus=1, cpu=8, mem="32GiB", image=GPU_VM_IMAGE):
    _valid_vm_name(name)
    user = user_get(owner)
    if not user or not user.get("ssh_keys"):
        raise ValueError(
            f"User '{owner}' has no SSH keys registered. Add one first:\n"
            f"  ycluster vm ssh add {owner} '<public-key>'")
    if vm_get(name) or _instance_exists(name):
        raise ValueError(f"VM '{name}' already exists.")

    free = free_gpus()
    if len(free) < gpus:
        raise ValueError(f"Only {len(free)} GPU(s) free, requested {gpus}.")
    picked = free[:gpus]

    print(f"Creating VM '{name}' ({cpu} CPU, {mem} RAM, {gpus} GPU) "
          f"owned by '{owner}'...")
    _incus("init", image, name, "--vm", "--profile", GPU_VM_PROFILE,
           "-c", f"limits.cpu={cpu}", "-c", f"limits.memory={mem}")
    for i, pci in enumerate(picked):
        _incus("config", "device", "add", name, f"gpu{i}", "gpu",
               "gputype=physical", f"pci={pci}")
        if pci.endswith(".0"):                 # pass the paired audio function
            _incus("config", "device", "add", name, f"gpu{i}snd", "pci",
                   f"address={pci[:-1]}1")
    _incus("start", name)

    _put_json(VMS_PREFIX + name, {
        "owner": owner,
        "gpus": gpus,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print("Waiting for the VM to boot, then injecting SSH keys...")
    _inject_owner_keys(name, user["ssh_keys"])
    bastion_sync()

    print(f"Launched '{name}' with GPU(s): {', '.join(picked)}")
    print(f"  ssh -J {BASTION_JUMP_USER}@<rathole-host>:2210 "
          f"{GPU_VM_GUEST_USER}@{name}")


def vm_stop(name):
    _incus("stop", name)
    print(f"Stopped '{name}'.")


def vm_start(name):
    _incus("start", name)
    print(f"Started '{name}'.")


def vm_destroy(name):
    if _instance_exists(name):
        _incus("delete", name, "--force")
    _delete(VMS_PREFIX + name)
    bastion_sync()
    print(f"Destroyed '{name}' and removed its registration.")


def vm_list():
    vms = vms_all()
    if not vms:
        print("No VMs registered.")
        return
    states = {i["name"]: i.get("status", "?")
              for i in _incus_json("list", "--format", "json")}
    print(f"{'NAME':<20} {'OWNER':<14} {'GPUS':<5} {'STATE':<10} CREATED")
    for name, rec in sorted(vms.items()):
        print(f"{name:<20} {rec.get('owner', '?'):<14} "
              f"{str(rec.get('gpus', '?')):<5} "
              f"{states.get(name, '(absent)'):<10} {rec.get('created', '')}")


def vm_sync_keys(user):
    """Re-inject a user's current keys into every running VM they own.

    If the user has no keys left (or the record is gone), the VMs'
    authorized_keys are emptied — removing a user's keys revokes access.
    """
    rec = user_get(user) or {"ssh_keys": []}
    keys = rec.get("ssh_keys", [])
    for name in _owner_vms().get(user, []):
        if _instance_exists(name):
            print(f"Refreshing keys in '{name}'...")
            _inject_owner_keys(name, keys)


# --------------------------------------------------------------------------
# bastion access list
# --------------------------------------------------------------------------
def _bastion_authorized_keys():
    """Render the jump user's authorized_keys from the etcd registries.

    Each key line is restricted to port-forwarding only, and `permitopen`
    is locked to the :22 of exactly the VMs that user owns.
    """
    users = users_all()
    owner_vms = _owner_vms()
    lines = ["# Generated by 'ycluster vm bastion-sync' — do not edit."]
    for owner, rec in sorted(users.items()):
        vmnames = sorted(owner_vms.get(owner, []))
        if not vmnames:
            continue                           # owns no VMs -> no jump access
        permits = ",".join(f'permitopen="{v}:22"' for v in vmnames)
        opts = f"restrict,port-forwarding,{permits}"
        for key in rec.get("ssh_keys", []):
            lines.append(f"{opts} {key}")
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
