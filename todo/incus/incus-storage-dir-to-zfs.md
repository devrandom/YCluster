# Incus storage dir → zfs

Migrate Incus storage pools from `dir` to `zfs` with per-VM
`refreservation` so a runaway guest can't fill the host root fs and
trigger the GPU-FLR cascade seen in production (the deployment incident
log has the detail).
Write the migration procedure (it's destructive — VMs get recreated).

Refs:
  the deployment incident log
