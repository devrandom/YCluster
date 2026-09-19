"""In-place VM alteration.

Split from :mod:`vm_manager` because both the operation and its supporting
validation helpers form a self-contained lifecycle surface.  vm_manager is
accessed through the module object so tests can patch the same seams as
before.
"""

from . import vm_manager as vm


def _validate_vm_alter(name, rec, image, instance_type, gpus, cur_type):
    """Reject alter requests that cannot be applied in place."""
    if not vm._instance_exists(name):
        raise ValueError(
            f"'{name}' is registered (host={rec.get('host')}) but not "
            f"present on this host ({vm.socket.gethostname()}). Run the "
            f"alter on {rec.get('host')}.")
    if image is not None:
        raise NotImplementedError(
            "Changing --image on an existing VM is not supported. "
            "Destroy and relaunch with the new image.")
    if instance_type is not None and instance_type != "auto" \
            and instance_type != cur_type:
        raise NotImplementedError(
            f"Changing --type from '{cur_type}' to '{instance_type}' on "
            f"an existing VM is not supported. Destroy and relaunch.")
    if gpus is not None and gpus < 0:
        raise ValueError("--gpus cannot be negative")


def _alter_owner(rec, owner):
    """Change the owner in the registry; validate that its keys exist."""
    if owner is None or owner == rec.get("owner"):
        return False

    new_user = vm.user_get(owner)
    if not new_user or not new_user.get("ssh_keys"):
        raise ValueError(
            f"New owner '{owner}' has no SSH keys registered. "
            f"Add one first: ycluster vm ssh add {owner} '<public-key>'")
    rec["owner"] = owner
    return True


def _altered_access(rec, add_users, remove_users):
    """Return (new_access_set, changed) after validating additional users."""
    cur_add = set(rec.get("additional_users") or [])
    add_users = add_users or []
    remove_users = remove_users or []

    for user in add_users:
        vm._valid_user(user)
        # Validating the user has keys mirrors the owner path: an added
        # user with no registered keys would grant nothing and confuse.
        user_rec = vm.user_get(user)
        if not user_rec or not user_rec.get("ssh_keys"):
            raise ValueError(
                f"User '{user}' has no SSH keys registered. Add one first: "
                f"ycluster vm ssh add {user} '<public-key>'")

    new_add = (cur_add | set(add_users)) - set(remove_users)
    owner_name = rec.get("owner")
    if owner_name:
        new_add.discard(owner_name)        # owner is never an additional user
    return new_add, new_add != cur_add


def _alter_vm_gpus(name, rec, cur_type, gpus):
    """Apply a GPU change; returns True when the registry value changes.

    Passthrough GPUs can only be detached while a VM is stopped, so this
    never restarts a running VM. Containers use the launch-style
    0/non-zero toggle on the profile device.
    """
    if gpus is None or gpus == (rec.get("gpus") or 0):
        return False

    if cur_type == "vm":
        if vm._instance_info(name).get("status") == "Running":
            raise ValueError(
                f"'{name}' is running; stop it before changing GPUs. "
                "Start it again after the alter to apply the new count.")
        vm.release_gpus(name)                 # detach current physical GPUs
        rec["gpus"] = gpus
        vm._put_json(vm.VMS_PREFIX + name, rec)
        if gpus:
            vm._ensure_gpus_attached(name)    # attach new count from pool
    else:
        # Container: gpu-ct profile bakes in a shared gpu0 device;
        # --gpus 0 strips it, any positive value keeps/adds it.
        if gpus == 0:
            vm._incus("config", "device", "remove", name, "gpu0", check=False)
        elif not any(d.get("type") == "gpu"
                     for d in (vm._instance_info(name).get("devices")
                               or {}).values()):
            # Profile device was stripped previously; re-attach from
            # the profile rather than pinning a fresh one (there is
            # only one shared GPU on a container host).
            vm._incus("config", "device", "add", name, "gpu0",
                   "gpu", "gputype=physical")
        rec["gpus"] = gpus
    return True


def _alter_resources(name, rec, cpu, mem):
    """Live-apply cpu and mem limits; returns (cpu_changed, mem_changed)."""
    cpu_changed = cpu is not None and str(cpu) != str(rec.get("cpu"))
    mem_changed = mem is not None and mem != rec.get("mem")
    if cpu_changed:
        vm._incus("config", "set", name, "limits.cpu", str(cpu))
        rec["cpu"] = cpu
    if mem_changed:
        vm._incus("config", "set", name, "limits.memory", mem)
        rec["mem"] = mem
    return cpu_changed, mem_changed


def _vm_alter_changes(owner, owner_changed, rec, access_changed,
                      gpus, gpu_changed, cpu, cpu_changed, mem, mem_changed):
    """Build the human-readable change summary for vm_alter."""
    changes = []
    if owner_changed:
        changes.append(f"owner -> {owner}")
    if access_changed:
        joined = ", ".join(rec.get("additional_users") or [])
        changes.append(f"additional users -> [{joined}]")
    if gpu_changed:
        changes.append(f"gpus -> {gpus}")
    if cpu_changed:
        changes.append(f"cpu -> {cpu}")
    if mem_changed:
        changes.append(f"mem -> {mem}")
    return changes


def vm_alter(name, owner=None, gpus=None, cpu=None, mem=None,
             image=None, instance_type=None, billable=False,
             add_users=None, remove_users=None):
    """Alter parameters of an existing VM. Pass only what should change.

    Live-applies: cpu, mem (mem visible to the guest on next reboot —
    Incus sets the qemu limit immediately but the in-guest kernel keeps
    its view until restart), owner and additional users (registry +
    bastion + key resync). For VMs, --gpus requires the VM to be stopped:
    it detaches the current physical GPUs and attaches the new count from
    the host pool, but never restarts a running VM. For containers --gpus
    is the same 0/non-zero toggle as launch (profile device add/remove);
    only the registry is updated.
    Not supported on an existing VM (would require destroy + relaunch):
    image, type.

    `add_users`/`remove_users` are lists of registered users granted
    (or stripped of) SSH access to the VM, alongside the owner. They must
    have keys registered to be added; the owner is never an additional
    user itself.

    No-op if every supplied value already matches the registry.
    """
    vm._valid_vm_name(name)
    rec = vm.vm_get(name)
    if not rec:
        raise ValueError(f"No registered VM named '{name}'.")
    cur_type = rec.get("type") or vm._default_instance_type()
    _validate_vm_alter(name, rec, image, instance_type, gpus, cur_type)
    if mem is not None:
        vm._validate_memory_size(mem)

    owner_changed = _alter_owner(rec, owner)
    new_add, access_changed = _altered_access(rec, add_users, remove_users)
    if access_changed:
        rec["additional_users"] = sorted(new_add)

    gpu_changed = _alter_vm_gpus(name, rec, cur_type, gpus)
    cpu_changed, mem_changed = _alter_resources(name, rec, cpu, mem)
    changes = _vm_alter_changes(
        owner, owner_changed, rec, access_changed, gpus, gpu_changed,
        cpu, cpu_changed, mem, mem_changed,
    )
    if not changes:
        print(f"No changes for '{name}' (every supplied value matches "
              f"the current registry).")
        return

    vm._put_json(vm.VMS_PREFIX + name, rec)
    if owner_changed or access_changed:
        # Recompute the VM's key set (owner + additional users) inline so
        # SSH works/revokes immediately; the bastion_watch daemon would
        # eventually reconcile naturally, but a manual alter should be
        # observable at once.
        vm._sync_vm_keys(name, vm.GPU_VM_AGENT_TIMEOUT if (rec.get("gpus") or 0)
                      else 180)
        vm.bastion_sync()

    vm._record_event("alter", name, owner=rec.get("owner"),
                  gpus=rec.get("gpus"), billable=billable)
    print(f"Altered '{name}': {', '.join(changes)}.")
