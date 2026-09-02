# Migrate VM data disks from virtiofs shares to block devices

## Problem

Guest VM data disks are served by `virtiofsd` from a NAS-mounted directory tree.
This places the host kernel's page-cache writeback machinery (folio allocation,
dirty tracking, `folio_wait_stable`) directly in the guest's I/O path. Two
incidents (a CIFS wedge, then an NFS hang) have shown that a NAS
stall wedges `vring_worker` in D state, cascading into a host-wide stall that
only a reboot clears.

Benchmark (sequential 1M blocks, iodepth 16, block-image-on-NFS vs host
O_DIRECT on the same share):

| | Host NFS (O_DIRECT) | Guest virtio-blk on NFS (`cache=none`) |
|---|---|---|
| Seq Write | 86 MB/s | 69 MB/s |
| Seq Read | 112 MB/s | 112 MB/s |

The ~20% write penalty is the cost of removing the wedge risk. Reads are
unaffected. See the private incidents directory for full analysis.

## Options

### Option A — Raw image on NFS (smallest change)

Create a raw disk image file on the existing NFS mount and attach it as a
virtio-blk device with `io.cache=none`.

**This is the only block-semantics option that requires zero per-VM
administration on the NAS.** `qemu-img create` writes the image from the
client side; no target, export, or ACL is needed on the server. New VM = one
client-side command plus an Incus device add.

Note: the NAS is a singleton bulk-tier server (spinning disk) — Ceph on SSD
serves the small-fast tier. Ceph RBD would be the ideal block backend for this
use case (client-side provisioning, native Incus driver, snapshots, thin
provisioning) but is not viable for large cheap VM data volumes on spinning
disks. Option A fills that gap: block semantics over the existing NAS with
no new server-side infrastructure.

```bash
qemu-img create -f raw /mnt/<pool>-vm/<vm>-data.img <size>
incus config device add <vm> data disk \
  source=/mnt/<pool>-vm/<vm>-data.img \
  io.cache=none io.bus=virtio-blk
```

- **Pros:** no new infrastructure; same NFS mount; guest sees a real block
  device and manages its own page cache; O_DIRECT bypasses host folio
  writeback entirely.
- **Cons:** still uses the NFS client (a server stall = slow guest I/O, not a
  wedge, but also no local resilience); ~20% write penalty; loses
  host-side file browsing inside the disk.
- **Effort:** low — per-VM device swap, no server changes.

### Option B — iSCSI target on the NAS

Run `targetcli` (LIO) on the NAS, export ZFS zvols as iSCSI LUNs, connect from
each GPU host with `open-iscsi`.

```bash
# NAS: create zvol + target
zfs create -s -V 50G <pool>/vm-<vm>-block
# ... targetcli configuration ...

# GPU host: connect
iscsiadm -m discovery -t sendtargets -p <nas-host>:3260
iscsiadm -m node --login
```

Attach as a local block device:

```bash
incus config device add <vm> data disk source=/dev/sdX \
  io.bus=virtio-blk
```

- **Pros:** removes NFS client code from the guest I/O path entirely; block
  protocol has no folio/writeback machinery; guest I/O goes directly over
  SCSI commands; standard enterprise pattern.
- **Cons:** new server-side infrastructure (targetcli, discovery, CHAP
  auth); new operational surface (persistent iSCSI sessions, multipath);
  NAS export no longer browsable as files; more failure modes to monitor.
- **Effort:** medium — requires playbook for both server and clients.

### Option C — NBD (Network Block Device)

Serve raw images from the NAS with `nbd-server`, connect on GPU hosts with
`nbd-client`. Simpler than iSCSI but less featureful (no multipath, no
persistent discovery).

```bash
# NAS: /etc/nbd-server/config
[generic]
[<vm>-data]
exportname = /<pool>/vm/<vm>-data.img

# GPU host:
nbd-client <nas-host> -N <vm>-data /dev/nbd0
```

- **Pros:** dead simple; no new storage subsystem; raw image already exists
  from Option A.
- **Cons:** single TCP connection (no `nconnect` equivalent); less mature
  ecosystem than iSCSI; no built-in multipath; nbd-client kernel module
  adds another wedge surface if server stalls.
- **Effort:** low-medium.

### Option D — ZFS zvol over network (hybrid)

A zvol is a raw block device backed by a ZFS dataset on the server
(the NAS already runs ZFS). Unlike a regular dataset (which holds files
and directories), a zvol appears on the server as
`/dev/zvol/<pool>/vm-<name>` — a virtual disk that can be partitioned,
formatted, or exported to another host via iSCSI or NBD. Its key
advantages over a plain file on ZFS are atomic snapshots
(`zfs snapshot <pool>/vm-<vm>@snap`), per-volume quota/tuning, and clean
block-level export without a filesystem layer in between on the server
side.

Create a zvol on the NAS and export it as a block device over iSCSI or NBD
(transport is handled by Option B or C).

```bash
# NAS:
zfs create -s -V 50G <pool>/vm-<vm>-block
# export the zvol's raw device via iSCSI or NBD
```

- **Pros:** ZFS copy-on-write snapshots of the zvol are cheap and
  consistent; refquota/recordsize tuning per VM.
- **Cons:** the zvol device node lives on the server, so client still needs
  NBD/iSCSI to access it as a block device — this collapses into Option B
  or C for the transport.
- **Effort:** medium-high; only makes sense combined with B or C.

### Option E — Local NVMe + rsync/backup to NAS

Give each VM a local NVMe-backed block volume and use periodic backup to the
NAS for durability. No network in the I/O path at all.

- **Pros:** best performance by far; no network stall risk; simplest
  reliability story.
- **Cons:** data is not live on the NAS — recovery requires restore;
  host NVMe capacity becomes the limit; no shared filesystem semantics.
- **Effort:** low for the device, medium for the backup workflow.

## Recommendation

**Option A is the recommended path** — not just as a stepping stone, but as
the durable answer for the bulk-tier NAS storage. The cluster's storage
architecture is two-tier: Ceph on SSD for small fast stateful services, and
a singleton spinning-disk NAS for large cheap VM data. Ceph RBD is not
viable for the bulk tier due to cost, and iSCSI requires per-VM server-side
administration that does not scale. Option A gives block semantics over the
existing NAS with no new infrastructure, and the benchmark shows the
reliability benefit costs only ~20% on sequential writes.

Migrate the VMs with the most wedge risk first (GPU VMs with heavy write
workloads).

## Implementation shape

Today `_ensure_nas_share()` in `vm_manager.py` unconditionally attaches
`/near1/vm/<name>` as a virtiofs disk named `data` (VMs) or a bind-mounted
directory (containers). Change it to prefer a block image when one exists:

- Image layout: one raw file per VM at
  `{{ vm_data_nfs_mount }}/<vm>.img` (i.e. `/mnt/near1-vm/<vm>.img` on the
  NFSv4 mount). Containers keep the directory share — block images are a
  VM-only change.
- Device config for a VM with an image:
  `incus config device add <vm> data disk source=<img> path=/data
  io.cache=none io.bus=virtio-blk`. `required=true` (the Incus default for
  disks) so a missing/unreachable image fails the start instead of booting
  the VM with no `/data`; this is deliberate — silently dropping the data
  disk is worse than a failed start.
- No uid mapping is needed at all — a raw block image has no filesystem on
  the host side, which is the point of the change. Keep `raw.idmap` unset
  either way (it destabilizes the legacy virtiofs path).
- Filesystem inside the image: **ext4**, labelled `vmdata`, directly on the
  block device (no partition table). Rationale: `resize2fs` grows online
  while mounted, it's the guest default so no new packages, and no
  partition-table step means no growpart dependency for resize.
- Migration is triggered automatically by `_ensure_nas_share()` on every VM
  start (see below); no separate CLI command, no per-VM manual checklist.
- Keep `qemu-img` on VM hosts (`install-incus.yml` already installs
  `qemu-system-x86`; add `qemu-utils` for `qemu-img`).

## Automatic host-side migration on start

The copy is entirely host-side — the VM is never started until the image is
populated and verified. The guest's first start on the new disk already has
all its data, so on-boot scripts and jobs that read `/data` behave normally.
No in-guest steps, no half-populated `/data`, no `data-legacy` device in the
guest.

`_ensure_nas_share()` becomes a two-way switch on VM start:

1. **Image exists** → attach the block image as `data` (config above). The
   atomic rename in step 2 guarantees a complete, verified image whenever the
   name exists — no partial-image state is ever visible.
2. **No image** → migrate on the host while the VM is still stopped:
   a. **Pre-flight.** VM is a VM (not a container), the NFS VM-data mount
      check passes, `<vm>.img` does not exist (refuse to overwrite), and NAS
      free space covers the target size.
   b. **Size the image** from the current share: `du -sm` (allocated) on
      the VM's directory × 1.5, rounded up, with a **floor of 200 GiB**.
      Surveyed on the NAS: VM data totals ~5.3 TiB allocated across all
      shares, NAS free space is ~19.5 TiB, and the largest share has ~30%
      sparse files (apparent size 1.3× allocated). Sizing by allocated is
      therefore right — but the copy must use `rsync -S` (`--sparse`) so
      holes in the source don't expand into allocated zeros inside the ext4
      image and blow past the allocated-based size. Sparse raw images make
      oversizing cheap on the ZFS-backed NAS, and the 50% headroom absorbs
      filesystem metadata (ext4 journal + inodes), growth between the size
      check and the copy, and near-term guest writes. The 200 GiB floor
      matches the root-disk default and gives every VM a reasonable data
      volume even if the legacy share was nearly empty.

   c. **Create and populate.** `qemu-img create -f raw <img>.tmp <size>`
      (no preallocation), `mkfs.ext4 -L vmdata` the image, loop-mount it,
      then `rsync -aHAXS` the NAS directory tree into the mount. Ownership
      inside the image is uid/gid 1000 (matching the single-UID squash on the
      NFS export and the guest's `ubuntu` user). While the image is
      loop-mounted, append a `/etc/fstab` entry
      (`LABEL=vmdata  /data  ext4  defaults  0  2`) so the guest mounts
      `/data` from the block device on every boot — virtiofs was
      Incus-managed and needed no fstab, but a raw block disk does. The
      legacy virtiofs share stays untouched — it is the source, and remains
      the rollback copy.
   d. **Verify, clean up, and commit.** Compare file counts and byte totals
      between source and target, unmount, detach the loop device, then
      `rename()` `<img>.tmp` → `<img>`. The rename is atomic on the same NFS
      export, so it is the completion marker: no state file, no etcd record,
      no ambiguous intermediate.
   e. **Swap devices while stopped.** Remove the legacy virtiofs device and
      add the block image as `data` at `/data` (config above). Then start the
      VM normally.
3. **Migration pre-flight fails** (NAS outage, insufficient space) → fall
   back to today's directory-share attach so the VM still boots on the
   legacy path; the next start retries the migration. A failed migration
   must not turn into a VM outage.

**Interruption safety:** a stale `<img>.tmp` from an interrupted copy is
detected on the next start, deleted, and the migration restarts from
scratch. The legacy directory is never modified until the image is verified
and the device swap has happened, so there is no point at which data can
diverge between source and target.

**Rollback:** after the device swap, the NAS directory tree still holds the
last-known-good copy until a housekeeping pass deletes it. Re-pointing `data`
back at the directory share restores the pre-migration state, though any
writes the guest made after the swap are lost — the same tradeoff as
deleting a backup too early, which is why housekeeping is deliberate.

**Rollout:** deploy the code, then let ordinary starts migrate VMs — one
low-risk VM first as the staged test, then the heavy-write GPU VMs (the
wedge-risk motivation). Run the alerting rules below from the first deploy.

## Resizing: `ycluster vm resize-data <vm> <size>`

The image is a fixed-size raw file — the directory share it replaces grew
implicitly with NAS free space, so an explicit resize path is required.
Grow-only (same constraint as root-disk resize). Requires the image to exist
(refuses on a VM still on the legacy directory share).

1. **Stop the VM** (or refuse if running — `qemu-img resize` of a running
   image is safe but the guest FS resize is not).
2. **Grow the image:** `qemu-img resize <img> <size>` on the host.
3. **Resize the FS.** The image has no partition table (ext4 directly on the
   block device, labelled `vmdata`), so there is no growpart step. Start the
   VM — the guest sees the new size at boot — then grow the mounted
   filesystem online: `incus exec <vm> -- resize2fs /dev/disk/by-label/vmdata`. Online
   `resize2fs` grow is supported on mounted ext4; the root requirement is
   guest-side (the ENOTSUP-on-virtiofs issue doesn't apply to a block
   device).
4. **Verify:** inside the guest, `df -h /data` shows the new size.

Shrink is explicitly out of scope — `resize2fs` cannot shrink online, and
shrinking a raw image risks data loss for no operational benefit. If a VM
needs a smaller data volume, destroy and re-launch.

## Alerting (do this regardless of option chosen)

- PromQL alert: `node_procs_blocked > 2 for 5m` on GPU hosts (early D-state).
- Log alert: `task vring_worker.*blocked` in kern.log (definitive signal).
- NAS I/O PSI: `rate(node_pressure_io_stalled_seconds_total{node="<nas>"}[5m]) > 0.05`.
- Node-exporter scrape duration: `scrape_duration_seconds > 5` on any GPU host.
