# NAS NFS mounts fail at boot and strand VM data

A GPU VM host's NAS-backed VM-data mount can fail during boot with
`mount to NFS server ... failed: No route to host`. Because the mount options
include `bg`, `mount.nfs` backgrounds the retry and exits; systemd sees no mount
at process exit and marks the unit `failed (Result: protocol)`. Incus auto-start
then fails with `Missing source path /mnt/nas-vm/<vm>.img`. The general NAS mount
on the same host can fail the same way.

## Root cause

- The NAS is not guaranteed reachable when the host reaches
  `network-online.target`. The target can be reached before the LAN link is
  stable, and `systemd-networkd-wait-online` can be skipped by its netplan
  condition. Its `--any -o routable` mode can also be satisfied by `wg0`
  instead of the LAN interface.
- `bg` is incompatible with a systemd `.mount` unit: the mount helper forks,
  the parent exits, and systemd records a protocol failure. The orphaned helper
  keeps retrying, but the unit does not recover.
- The host's 10G link has flapped and renegotiated at 1G, so the path to the
  NAS can also drop independently of NAS availability.

## Recommended fixes

1. **Remove `bg` and add a retry path.** `hard` keeps the mount retrying;
   a per-mount retry starts a failed mount unit every 30s. This also
   recovers after a NAS outage longer than the mount timeout.
   - Status: implemented in `group_vars/all/main.yml`,
     `host_vars/<host>.yml`, and `mount-nas-share.yml`; deployed on the
     affected host. Both NFS mounts are active.
   - Revised: the retry is one service and timer per mount, gated by
     `ExecCondition=/bin/sh -c '! mountpoint -q <path>'`, so a healthy host
     never starts a mount unit and the IDS sees only the read-only probe.
     systemd 255 has no mount-point condition (`ConditionPathIsMountPoint`
     is rejected as an unknown key).
2. **Order VM startup after the mount.** Add
   `RequiresMountsFor=<vm-data mount>` to `incus.service` and
   `vm-reconciler.service`, or a dedicated dependency unit, so auto-start
   does not race the mount.
   - Status: implemented as ordering, not a hard requirement:
     per-mount drop-ins add `Wants=` and `After=` on the mount unit to
     `incus.service` and `vm-reconciler.service`. `RequiresMountsFor=` would
     make incusd fail its start job whenever the NAS is down; with
     `Restart=on-failure` that hits systemd's start limit and can leave
     incusd failed after the NAS returns. Ordering removes the race and
     keeps the control plane up; the reconciler retries instance starts
     once the mount recovers. Deployed on the affected host.
3. **Make `network-online.target` real.** Fix the netplan wait-online
   condition so `systemd-networkd-wait-online` runs, and wait for the LAN
   interface rather than `--any -o routable`.
   - Status: open.
4. **Ensure the NAS is up before the host boots.** If the NAS can be down
   for hours, the mount retry is the fallback; power/boot ordering is the
   primary fix.
   - Status: open.
5. **Apply the same mount-option correction to the general NAS mount.**
   The general mount uses the same `bg` option and fails the same way.
   - Status: implemented with item 1 and deployed on the affected host; the
     general mount is active with the corrected options.

## Done when

- No NFS mount unit uses `bg`.
- A failed mount is retried automatically and recovers without a manual
  `systemctl start`.
- Incus auto-start either waits for the VM-data mount or retries after it
  becomes available.
- `systemd-networkd-wait-online` waits for the LAN interface used by the NAS.
- The host link stays at its expected speed without flaps.

## Test

- `systemd-analyze verify` the generated mount, retry service, and timer.
- Deploy `app/mount-nas-share.yml --limit <host>`; verify both mounts are
  active, the retry timer is active, and the units contain no `bg`.
- Reboot with the NAS unreachable, then make it reachable and verify the
  mounts recover without manual intervention and the VM starts.
