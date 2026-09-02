"""NAS-backed VM data image migration, attach, and resize helpers.

The VM stays stopped while the legacy directory share is copied, verified,
and atomically renamed to the final image. The legacy share is never modified;
it remains the rollback copy.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager

NAS_NFS_MOUNT = "/mnt/near1-vm"
NAS_HOST_BASE = "/near1/vm"
# ext4, no partition table: resize2fs grows online and no growpart step is
# needed. The floor leaves room for metadata and near-term guest growth.
VM_DATA_MIN_BYTES = 200 * 1024 * 1024 * 1024
VM_DATA_FSTAB_ENTRY = "LABEL=vmdata  /data  ext4  defaults  0  2\n"

# Incus disk device carrying the VM data image.
VM_DATA_DEVICE = "data"
VM_DATA_GUEST_PATH = "/data"
# The image's ext4 label is the stable handle for guest-side operations:
# device indexes (/dev/vdb) shift when other disks are attached.
VM_DATA_RESIZE_DEVICE = "/dev/disk/by-label/vmdata"

LOCK_DIR = "/run/lock"


def data_image_path(name):
    """Completed data image path for `name` (the migration commit marker)."""
    return f"{NAS_NFS_MOUNT}/{name}.img"


def migration_size_mib(allocated_mib):
    """Size a data image from allocated bytes, with a 200 GiB floor."""
    return max(VM_DATA_MIN_BYTES // (1024 * 1024),
               (allocated_mib * 3 + 1) // 2)


def parse_disk_size(size):
    """Parse a qemu-style disk size (for example 200GiB or 1TiB).

    All suffixes are binary (K=1024, M=1024², ...); KB/MB/GB/TB are accepted
    as aliases for KiB/MiB/GiB/TiB. The exact byte count is passed to
    qemu-img, so the parse only needs to be internally consistent.
    """
    m = re.fullmatch(r"(\d+)(B|[KMGT](?:i?B)?)", size.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(
            f"Invalid disk size {size!r}; use a positive size such as 200GiB")
    unit = m.group(2).upper()
    unit_bytes = {"B": 1, "K": 1024, "KB": 1024, "KIB": 1024,
                  "M": 1024**2, "MB": 1024**2, "MIB": 1024**2,
                  "G": 1024**3, "GB": 1024**3, "GIB": 1024**3,
                  "T": 1024**4, "TB": 1024**4, "TIB": 1024**4}[unit]
    return int(m.group(1)) * unit_bytes


@contextmanager
def data_migration_lock(name, lock_dir=None):
    """Cross-process per-VM lock around migration and the device swap.

    A concurrent start blocks here until the image is committed and attached,
    so it can never boot against the legacy share mid-copy and then be
    switched to a stale image.
    """
    os.makedirs(lock_dir or LOCK_DIR, exist_ok=True)
    path = os.path.join(lock_dir or LOCK_DIR, f"ycluster-vm-data-{name}.lock")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _regular_file_stats(path):
    """Return (count, bytes) for regular files, matching rsync's copy set."""
    r = subprocess.run(["find", path, "-type", "f", "-printf", "%s\\n"],
                       check=True, capture_output=True, text=True)
    sizes = [int(line) for line in r.stdout.splitlines()]
    return len(sizes), sum(sizes)


def _append_vmdata_fstab(mountpoint):
    fstab = os.path.join(mountpoint, "etc", "fstab")
    current = ""
    if os.path.exists(fstab):
        with open(fstab, "r", encoding="utf-8") as f:
            current = f.read()
    if "LABEL=vmdata" in current:
        return
    with open(fstab, "a", encoding="utf-8") as f:
        f.write(VM_DATA_FSTAB_ENTRY)


def _release_image_mount(loop_dev, mountpoint):
    if loop_dev:
        subprocess.run(["losetup", "-d", loop_dev], check=False,
                       capture_output=True, text=True)
    if mountpoint:
        subprocess.run(["rmdir", mountpoint], check=False,
                       capture_output=True, text=True)


def migrate_vm_data_to_image(name, image, *, is_running):
    """Populate `<image>.tmp` and atomically rename it to `<image>`.

    `is_running` is supplied by the caller because Incus state is owned by
    vm_manager. Every failure before the rename leaves the legacy share and
    the final image name untouched. Verification compares file counts and
    byte totals — it catches truncation, not same-size corruption; the
    untouched legacy share is the rollback copy.
    """
    if is_running():
        raise RuntimeError("VM is running; migration only runs while stopped")
    r = subprocess.run(["mountpoint", "-q", NAS_NFS_MOUNT], check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{NAS_NFS_MOUNT} is not an active mount")

    source = f"{NAS_NFS_MOUNT}/{name}"
    if not os.path.isdir(source):
        raise RuntimeError(f"legacy data directory {source} is missing")
    if os.path.exists(image):
        raise RuntimeError(f"data image already exists: {image}")

    r = subprocess.run(["du", "-sm", source], check=True,
                       capture_output=True, text=True)
    size_mib = migration_size_mib(int(r.stdout.split()[0]))
    st = os.statvfs(NAS_NFS_MOUNT)
    free_bytes = st.f_bavail * st.f_frsize
    if free_bytes < size_mib * 1024 * 1024:
        raise RuntimeError(
            f"NAS has {free_bytes // (1024**3)} GiB free; need "
            f"{size_mib // 1024} GiB")

    tmp = f"{image}.tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)

    subprocess.run(["qemu-img", "create", "-f", "raw", tmp, f"{size_mib}M"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["mkfs.ext4", "-F", "-L", "vmdata", tmp], check=True,
                   capture_output=True, text=True)

    loop_dev = None
    mountpoint = None
    try:
        r = subprocess.run(["losetup", "--find", "--show", tmp],
                           check=True, capture_output=True, text=True)
        loop_dev = r.stdout.strip()
        mountpoint = tempfile.mkdtemp(prefix=f"{name}.data-", dir="/mnt")
        subprocess.run(["mount", loop_dev, mountpoint], check=True,
                       capture_output=True, text=True)
        subprocess.run(["rsync", "-aHAXS", f"{source}/", f"{mountpoint}/"],
                       check=True, capture_output=True, text=True)

        source_stats = _regular_file_stats(source)
        target_stats = _regular_file_stats(mountpoint)
        if source_stats != target_stats:
            raise RuntimeError(
                f"copy verification failed: source files/bytes "
                f"{source_stats[0]}/{source_stats[1]}, target "
                f"{target_stats[0]}/{target_stats[1]}")
        _append_vmdata_fstab(mountpoint)

        subprocess.run(["umount", mountpoint], check=True,
                       capture_output=True, text=True)
        subprocess.run(["losetup", "-d", loop_dev], check=True,
                       capture_output=True, text=True)
        loop_dev = None
        os.rmdir(mountpoint)
        mountpoint = None
        # Last stopped-state recheck before the commit point: if the VM was
        # started while the copy ran, the legacy share is now the live data
        # and a renamed image would diverge from it.
        if is_running():
            raise RuntimeError(
                "VM started during migration; keeping the legacy share")
        os.replace(tmp, image)
    finally:
        if mountpoint:
            subprocess.run(["umount", mountpoint], check=False,
                           capture_output=True, text=True)
        _release_image_mount(loop_dev, mountpoint)
        if os.path.exists(tmp):
            os.unlink(tmp)


def replace_data_device(incus, name, source, *extra):
    """Attach `source` as the `data` disk, replacing a different backing."""
    r = incus("config", "device", "get", name, VM_DATA_DEVICE, "source",
              check=False)
    current = r.stdout.strip() if r.returncode == 0 else None
    if current == source:
        return
    if current is not None:
        incus("config", "device", "remove", name, VM_DATA_DEVICE)
    incus("config", "device", "add", name, VM_DATA_DEVICE, "disk",
          f"source={source}", f"path={VM_DATA_GUEST_PATH}", *extra)


def assert_resizable(incus, name, image):
    """Refuse resize unless the data device is the committed image.

    A legacy-attached VM means migration never completed; growing the image
    anyway would leave the guest's writes in the wrong volume.
    """
    if not os.path.exists(image):
        raise ValueError(f"No data image for '{name}'; start it once to "
                         "migrate or attach an existing image")
    r = incus("config", "device", "get", name, VM_DATA_DEVICE, "source",
              check=False)
    if r.returncode != 0 or r.stdout.strip() != image:
        raise ValueError(
            f"data device for '{name}' is not the data image; start the VM "
            "once to complete migration")


def grow_data_image(image, new_bytes):
    """Grow a raw data image to `new_bytes`; refuse to shrink."""
    info = json.loads(subprocess.run(
        ["qemu-img", "info", "--output=json", image], check=True,
        capture_output=True, text=True).stdout)
    current_bytes = int(info["virtual-size"])
    if new_bytes < current_bytes:
        raise ValueError(
            f"Data images can only grow: current size is {current_bytes} "
            f"bytes, requested {new_bytes}")
    if new_bytes > current_bytes:
        subprocess.run(["qemu-img", "resize", image, str(new_bytes)],
                       check=True, capture_output=True, text=True)
    return current_bytes


def ensure_nas_share(name, incus, *, share_enabled,
                     instance_info, instance_running):
    """Attach the VM data disk, migrating from virtiofs to block on start.

    For VMs, the image path is the completion marker for a verified
    host-side copy. If it exists, attach it as virtio-blk. If not, run the
    host-side migration while the VM is stopped; any failure falls back to
    the legacy directory share so the VM still boots (a VM that is already
    running keeps its current device). Containers always keep the directory
    share.
    """
    if not share_enabled:
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

    inst = instance_info(name)
    if not (inst or {}).get("type") == "virtual-machine":
        replace_data_device(incus, name, host_dir, "required=false")
        return

    image = data_image_path(name)
    try:
        with data_migration_lock(name):
            if not os.path.exists(image):
                migrate_vm_data_to_image(
                    name, image,
                    is_running=lambda: instance_running(name))
            replace_data_device(incus, name, image, "io.cache=none",
                                "io.bus=virtio-blk", "required=true")
    except Exception as e:
        if os.path.exists(image):
            # The committed image is the source of truth: booting the legacy
            # share now would diverge from the image and make it stale.
            raise
        print(f"  VM data migration skipped for '{name}': {e}",
              file=sys.stderr)
        if not instance_running(name):
            replace_data_device(incus, name, host_dir, "required=false")
