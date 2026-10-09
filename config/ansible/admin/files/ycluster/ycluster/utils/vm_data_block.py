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

from .host_config import host_config_value


def _host_path(key, default):
    return host_config_value(key) or default


# VM-data paths are per-host (written into host.yml by install-incus.yml) so
# hosts can source VM data from any NAS export. Defaults preserve the
# historical layout for hosts whose host.yml predates the keys.
NAS_NFS_MOUNT = _host_path("vm_data_mount", "/mnt/near1-vm")
NAS_HOST_BASE = _host_path("vm_data_host_base", "/near1/vm")
# ext4, no partition table: resize2fs grows online and no growpart step is
# needed. The floor leaves room for metadata and near-term guest growth.
VM_DATA_MIN_BYTES = 200 * 1024 * 1024 * 1024

# Incus disk device carrying the VM data image.
VM_DATA_DEVICE = "data"
VM_DATA_GUEST_PATH = "/data"
# The image's ext4 label is the stable handle for guest-side operations:
# device indexes (/dev/vdb) shift when other disks are attached.
VM_DATA_RESIZE_DEVICE = "/dev/disk/by-label/vmdata"
VM_DATA_FSTAB_LINE = "LABEL=vmdata  /data  ext4  defaults  0  2"

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


def _release_image_mount(loop_dev, mountpoint):
    if loop_dev:
        subprocess.run(["losetup", "-d", loop_dev], check=False,
                       capture_output=True, text=True)
    if mountpoint:
        subprocess.run(["rmdir", mountpoint], check=False,
                       capture_output=True, text=True)


def _legacy_share_has_data(host_dir):
    """True when a legacy directory share exists with anything in it.

    Uses the same abandon-on-timeout probe pattern as `_nas_reachable`:
    the legacy base can live on a different mount from the image mount,
    and a dead hard mount must never wedge `vm start`.
    """
    try:
        proc = subprocess.Popen(["ls", "-A", "--", host_dir],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return False
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and bool(out.strip())


def create_data_image(name, image):
    """Create an empty labelled ext4 data image at the 200 GiB floor.

    Used for fresh VMs that have no legacy directory share to migrate.
    The caller holds the per-VM data migration lock and ensures no image
    exists yet; the VM must not be running. Built in a `.tmp` sibling and
    atomically renamed so a mid-format failure never leaves a broken image
    at the final path.
    """
    size_mib = VM_DATA_MIN_BYTES // (1024 * 1024)
    st = os.statvfs(NAS_NFS_MOUNT)
    free_bytes = st.f_bavail * st.f_frsize
    if free_bytes < size_mib * 1024 * 1024:
        raise RuntimeError(
            f"NAS has {free_bytes // (1024**3)} GiB free; need "
            f"{size_mib // 1024} GiB")
    tmp = f"{image}.tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    try:
        subprocess.run(["qemu-img", "create", "-f", "raw", tmp,
                        f"{size_mib}M"], check=True, capture_output=True,
                       text=True)
        subprocess.run(["mkfs.ext4", "-F", "-L", "vmdata", tmp], check=True,
                       capture_output=True, text=True)
        os.replace(tmp, image)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


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
        # No capture: --info=progress2 streams live progress to the
        # terminal, and errors surface directly instead of being buffered.
        subprocess.run(
            ["rsync", "-aHAXS", "--info=progress2",
             f"{source}/", f"{mountpoint}/"],
            check=True)

        source_stats = _regular_file_stats(source)
        target_stats = _regular_file_stats(mountpoint)
        if source_stats != target_stats:
            raise RuntimeError(
                f"copy verification failed: source files/bytes "
                f"{source_stats[0]}/{source_stats[1]}, target "
                f"{target_stats[0]}/{target_stats[1]}")

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
    """Attach `source` as the `data` disk, replacing a different backing.

    Callers must pass `path=` themselves: block-image sources reject `path=`
    on VMs, while directory sources need it.
    """
    r = incus("config", "device", "get", name, VM_DATA_DEVICE, "source",
              check=False)
    current = r.stdout.strip() if r.returncode == 0 else None
    if current == source:
        return
    if current is not None:
        incus("config", "device", "remove", name, VM_DATA_DEVICE)
    incus("config", "device", "add", name, VM_DATA_DEVICE, "disk",
          f"source={source}", *extra)


def ensure_guest_mount(incus, name):
    """fstab + mount `/data` in the guest for an image-backed data device.

    Incus VMs reject `path=` on non-directory disk sources, so the guest
    mounts the image itself. Idempotent; no-op unless the data device is
    the block image (directory-backed shares are mounted by Incus and
    containers don't run an agent).

    The mount happens before the fstab append: a fstab entry for a device
    that failed to mount would risk boot hangs if the image is ever
    detached. Without the fstab entry the guest still works — /data is
    just unmounted until the next `ensure_guest_mount` call.
    """
    r = incus("config", "device", "get", name, VM_DATA_DEVICE, "source",
              check=False)
    if r.returncode != 0 or r.stdout.strip() != data_image_path(name):
        return
    script = (
        f"set -e; mkdir -p {VM_DATA_GUEST_PATH}; "
        f"mountpoint -q {VM_DATA_GUEST_PATH} || "
        f"mount LABEL=vmdata {VM_DATA_GUEST_PATH}; "
        f"grep -q '^LABEL=vmdata' /etc/fstab || "
        f"echo '{VM_DATA_FSTAB_LINE}' >> /etc/fstab")
    incus("exec", name, "--", "sh", "-c", script)


def migration_note(image_exists, legacy_dir_exists):
    """NOTES-column value for `vm list` describing migration state."""
    if image_exists:
        return ""
    return "unmigrated" if legacy_dir_exists else ""


def guest_mount_after_boot(incus, name, wait_agent, agent_timeout):
    """Wait for the agent, then fstab+mount /data; warn (don't raise) on
    failure — the VM itself started fine, and `vm_resize_data` re-runs
    `ensure_guest_mount` as a self-heal."""
    try:
        print(f"  Waiting for '{name}' agent to configure /data...")
        wait_agent(name, agent_timeout)
        ensure_guest_mount(incus, name)
        print(f"  /data in '{name}' now comes from the block image.")
    except Exception as e:
        print(f"  WARNING: /data mount setup failed for '{name}': {e}",
              file=sys.stderr)


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


def grow_data_image_and_fs(image, new_bytes):
    """Grow a raw ext4 data image and its filesystem; refuse to shrink.

    The caller must hold the per-VM data migration lock and ensure the VM is
    stopped. Keeping the filesystem resize on the host avoids an implicit VM
    start/restart just to run resize2fs in the guest.
    """
    old_bytes = grow_data_image(image, new_bytes)
    r = subprocess.run(
        ["losetup", "--find", "--show", image],
        check=True, capture_output=True, text=True)
    loop_dev = r.stdout.strip()
    try:
        subprocess.run(["e2fsck", "-fp", loop_dev], check=True,
                       capture_output=True, text=True)
        subprocess.run(["resize2fs", loop_dev], check=True,
                       capture_output=True, text=True)
    finally:
        _release_image_mount(loop_dev, None)
    return old_bytes


def _nas_reachable():
    """Probe the NAS mount without ever blocking on a dead hard mount.

    A hard NFS mount can leave any accessor stuck in uninterruptible D-state
    until the server returns. SIGKILL cannot break that, so we don't wait()
    on the probe: poll for 10s and, if it hasn't exited, abandon the orphan
    and report unreachable. The orphan stays a zombie until this (short-lived
    CLI) process exits; init reaps it then, once the syscall returns.
    """
    try:
        proc = subprocess.Popen(["ls", NAS_NFS_MOUNT],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"  NAS probe failed to start ({e.__class__.__name__}).",
              file=sys.stderr)
        return False
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(f"  NAS {NAS_NFS_MOUNT} unreachable (probe timed out) — "
              f"skipping /data attach.", file=sys.stderr)
        return False
    if rc != 0:
        print(f"  NAS {NAS_NFS_MOUNT} unreachable (ls rc={rc}) — "
              f"skipping /data attach.", file=sys.stderr)
        return False
    return True


def ensure_nas_share(name, incus, *, share_enabled, migrate=False,
                     instance_info, instance_running):
    """Attach the VM data disk for `name`.

    For VMs, an existing image (`<mount>/<name>.img`) is attached as
    virtio-blk. With no image and a non-empty legacy directory share,
    migration is opt-in (`migrate=True`); with no image and no legacy data a
    fresh block image is created directly. Containers always keep the
    directory share. The legacy directory is only created when it is the
    thing being attached or migrated — never for image-backed VMs.

    Returns True when migration ran in this call — the caller should run
    `ensure_guest_mount` after the first boot, since the guest needs a new
    fstab entry for the image-backed device.
    """
    if not share_enabled:
        return False
    if not _nas_reachable():
        return False
    host_dir = f"{NAS_HOST_BASE}/{name}"

    inst = instance_info(name)
    if not (inst or {}).get("type") == "virtual-machine":
        try:
            subprocess.run(["mkdir", "-p", host_dir], check=True, timeout=10,
                           capture_output=True, text=True)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                OSError) as e:
            print(f"  NAS {NAS_HOST_BASE} unreachable — skipping /data attach "
                  f"on '{name}' ({e.__class__.__name__}).", file=sys.stderr)
            return False
        replace_data_device(incus, name, host_dir, "required=false",
                            f"path={VM_DATA_GUEST_PATH}")
        return False

    image = data_image_path(name)
    migrated = False
    try:
        with data_migration_lock(name):
            if os.path.exists(image):
                replace_data_device(incus, name, image, "io.cache=none",
                                    "io.bus=virtio-blk", "required=true")
                return migrated

            if _legacy_share_has_data(host_dir):
                if not migrate:
                    # Migration is opt-in (`vm start --migrate`) whenever a
                    # real legacy share exists: never convert implicitly.
                    replace_data_device(incus, name, host_dir,
                                        "required=false",
                                        f"path={VM_DATA_GUEST_PATH}")
                    return False
                print(f"  Migrating '{name}' data to a block image "
                      f"(legacy share is kept until verified)...")
                migrate_vm_data_to_image(
                    name, image,
                    is_running=lambda: instance_running(name))
                migrated = True
                print(f"  Data image for '{name}' is ready; attaching.")
            else:
                # Fresh VM: no legacy data to preserve, so create the
                # image directly instead of a directory share.
                print(f"  Creating block image for '{name}' data...")
                create_data_image(name, image)
                print(f"  Data image for '{name}' is ready; attaching.")
            replace_data_device(incus, name, image, "io.cache=none",
                                "io.bus=virtio-blk", "required=true")
    except Exception as e:
        if os.path.exists(image):
            # The committed image is the source of truth: booting the legacy
            # share now would diverge from the image and make it stale.
            raise
        print(f"  VM data migration skipped for '{name}': {e}",
              file=sys.stderr)
        # Fall back to the legacy share only when it actually exists
        # (a real migration attempt). A fresh VM whose image creation
        # failed has no fallback — start without /data rather than
        # attaching a nonexistent source.
        if not instance_running(name) and os.path.isdir(host_dir):
            replace_data_device(incus, name, host_dir, "required=false",
                                f"path={VM_DATA_GUEST_PATH}")
        return False
    return migrated
