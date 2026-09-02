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

## Migration checklist (per VM)

1. Stop the VM.
2. `qemu-img create -f raw /mnt/<pool>-vm/<vm>-data.img <size>` on the host.
3. Add the new block device (Option A config above).
4. Start the VM with both devices.
5. Inside the guest: format, mount, `rsync` from old share to new disk.
6. Verify data, stop the VM.
7. Remove the old virtiofs `data` device.
8. Start and verify.

## Alerting (do this regardless of option chosen)

- PromQL alert: `node_procs_blocked > 2 for 5m` on GPU hosts (early D-state).
- Log alert: `task vring_worker.*blocked` in kern.log (definitive signal).
- NAS I/O PSI: `rate(node_pressure_io_stalled_seconds_total{node="<nas>"}[5m]) > 0.05`.
- Node-exporter scrape duration: `scrape_duration_seconds > 5` on any GPU host.
