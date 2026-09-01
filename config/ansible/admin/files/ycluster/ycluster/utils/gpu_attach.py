"""GPU attach/detach for passthrough VMs.

Split from :mod:`vm_manager` to keep each module focused; accesses the
parent module through ``vm.`` so test patches on vm_manager still apply.
"""

from . import vm_manager as vm


def _gpu_devices(inst):
    """Return {device_name: device_dict} for GPU devices with a PCI address."""
    devices = inst.get("devices") or {}
    return {d_name: d for d_name, d in devices.items()
            if isinstance(d, dict) and d.get("type") == "gpu" and d.get("pci")}


def _detach_excess_gpus(name, gpu_devs, want):
    """Remove the first (want - have) excess GPU devices, sorted by name."""
    excess = len(gpu_devs) - want
    for d_name in sorted(gpu_devs)[:excess]:
        vm._incus("config", "device", "remove", name, d_name)


def _attach_missing_gpus(name, devices, want, have):
    """Attach GPUs from the free pool until the VM holds `want` of them."""
    missing = want - have
    if missing <= 0:
        return
    try:
        picked = vm._select_free_gpus(missing)
    except ValueError as e:
        raise RuntimeError(f"'{name}' needs {missing} more GPU(s): {e}")
    idx = 0
    for pci in picked:
        while f"gpu{idx}" in devices:
            idx += 1
        vm._incus("config", "device", "add", name, f"gpu{idx}", "gpu",
               "gputype=physical", f"pci={pci}")
        devices[f"gpu{idx}"] = {"type": "gpu", "pci": pci}
        print(f"  re-attached GPU {pci} as gpu{idx}")


def _ensure_gpus_attached(name, want=None):
    """Attach passthrough GPUs from the host pool until the VM holds
    `want` of them (default: its registered count) — the inverse of
    release_gpus, run before every start. No-op for containers and
    unregistered instances. Any GPUs already attached beyond `want` are
    detached, so a start that asks for fewer than registered (the
    schedule page / `vm start --gpus N`) converges to exactly that many.
    Raises when the pool can't cover the gap.
    """
    inst = vm._instance_info(name)
    if not inst or inst.get("type") != "virtual-machine":
        return
    if want is None:
        want = (vm.vm_get(name) or {}).get("gpus") or 0
    gpu_devs = _gpu_devices(inst)
    have = len(gpu_devs)
    if have > want:
        _detach_excess_gpus(name, gpu_devs, want)
        return
    _attach_missing_gpus(name, inst.get("devices") or {}, want, have)
