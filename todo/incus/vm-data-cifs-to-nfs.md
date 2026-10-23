# Incus VM passthrough `data` disks: CIFS → NFSv4

A host-side CIFS + netfslib folio-lock writeback deadlock wedged a GPU VM's
`data` passthrough disk: its `virtiofsd` worker stuck D-state on
`cifs_reopen_file(can_flush=true) -> filemap_write_and_wait -> netfs_writepages`,
and only a host reboot clears a D-state thread. Full detail (private) lives in
the matching incident report under `../ycluster-private/incidents/`.

The deadlock is CIFS-specific. Moving the VM-data file protocol to NFSv4 removes
the buggy client path entirely and works on the current 7.0 HWE kernel — no
kernel downgrade and no `cache=none` perf penalty.

**Design: one NFS export/mount of the entire `/near1/vm` subtree.** Confirmed
`/near1/vm/` on the NAS host is a clean per-VM-data tree (one directory per
VM); everything above it (`/near1/comfyui`, `/near1/models`, `/near1/osv-feed`,
`/near1/ycluster-private`) stays on CIFS. So the GPU host mounts the single NFS
path and each Incus `data` disk is a bind mount beneath it (same shape as
today) — not one NFS mount per VM.

## Implementation status

- **Server: deployed on the NAS host** (`setup-nas.yml` ran against it;
  `nfs-kernel-server` + `rpcbind` active, `/etc/exports` live, `exportfs -v`
  and `showmount -e` confirm `/near1/vm` for `10.0.0.0/24`).
- **Client: deployed on the rollout GPU host** (`mount-nas-share.yml --limit <rollout-host>`; the NFS
  mount is live at `/mnt/near1-vm` — `vers=4.2,nconnect=4`, unit enabled at
  boot, check script installed and passing). Incus `data` disks still point
  at the CIFS bind-mount path; the repoint is deferred to the maintenance
  window.
- **Code:** `config/ansible/app/setup-nas.yml` installs `nfs-kernel-server`
  and always exports `/near1/vm` (single-UID squash
  `all_squash,anonuid=1000,anongid=1000`, net `10.0.0.0/24`).
- **Client (rollout switch):** `config/ansible/app/mount-nas-share.yml` gates
  the NFS client mount on the per-host Ansible flag `vm_data_nfs` (default
  `false`; the rollout host sets it `true` in its `host_vars/<host>.yml`). It
  installs `nfs-common`, computes the unit name from the mount path with
  `systemd-escape`, writes a static mount unit with `TimeoutSec=60` (no
  automount), enables/starts it, and fails if the mount is missing.
- **Mount check (for scripts):**
  `config/ansible/scripts/check-vm-data-nfs-mount.sh` → installed at
  `/usr/local/bin/check-vm-data-nfs-mount`; scripts call it before touching VM
  `data` paths.
- Client flags (see below) live in `group_vars/all/main.yml` as
  `vm_data_nfs_*`.

Still to do: staged test (hand mount + Incus `data` repoint) and rollout in the
maintenance window (see "Test").

## Done when

- `nfs-common` installed on each GPU host that mounts VM data (NFS client).
- `nfs-kernel-server` + an export for the **VM-data subtree only** on the NAS
  host, UID/GID-mapped to match today's CIFS `uid=1000,gid=1000,forceuid,
  forcegid` (single-UID mapping via `anonuid=1000,anongid=1000,all_squash`).
- Incus `data` disk `source` repointed from the CIFS bind-mount path to an NFSv4
  mount scoped to the VM-data subtree, while the shared NAS export stays CIFS
  for its existing consumers.
- Staged/tested (hand mount + an Incus `data` repoint) before prod; landed in
  the same maintenance window as the host reboot.

## Proposed client flags

```
nfsvers=4.2,hard,rsize=1048576,wsize=1048576,nconnect=4,actimeo=60,nocto,noatime,async
```

- `hard` — never surface a fake EIO to the guest on a transient blip.
- `nconnect=4` — 10G link; multiple TCP connections to saturate it.
- **No automount** (VM data must be present whenever virtiofsd starts); mount
  statically. Do **not** use `bg`: it makes `mount.nfs` fork, systemd sees no
  mount at process exit, and the unit fails. A `nas-nfs-mount-retry.timer` starts
  failed enabled mounts every 30s; the systemd mount unit caps each attempt with
  `TimeoutSec=60`.
- `_netdev` is **not** in the client options; the `.mount` unit orders itself
  after `network-online.target` via `After=`/`Wants=` instead.
- Keep the shared NAS export's CIFS automount untouched.

## Test

- Hand-mount the NFS path, run a VM `data` passthrough (guest write/read /
  reboot) before any prod remount.
- Verify `mount`/`nfsstat` and that the Incus device resolves to the NFS path
  after a host reboot.
