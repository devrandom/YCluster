# Incus storage dir → zfs

Migrate Incus storage pools from `dir` to `zfs` with per-VM
`refreservation` so a runaway guest can't fill the host root fs and
trigger the GPU-FLR cascade seen on nv3 (incident:
`../ycluster-private/incidents/2026-05-21 — nv3 GPU/driver-loss cascade`).
Write the migration procedure (it's destructive — VMs get recreated).

Refs:
  detail under the 2026-05-22 audit)
