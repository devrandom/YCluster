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
nfsvers=4.2,hard,bg,rsize=1048576,wsize=1048576,nconnect=4,actimeo=60,nocto,noatime,async,_netdev
```

- `hard` — never surface a fake EIO to the guest on a transient blip.
- `nconnect=4` — 10G link; multiple TCP connections to saturate it.
- **No automount** (VM data must be present whenever virtiofsd starts); mount
  statically. `bg` retries in the background instead of blocking boot; cap boot
  with `x-systemd.mount-timeout=60`.
- Keep the shared NAS export's CIFS automount untouched.

## Test

- Hand-mount the NFS path, run a VM `data` passthrough (guest write/read /
  reboot) before any prod remount.
- Verify `mount`/`nfsstat` and that the Incus device resolves to the NFS path
  after a host reboot.
