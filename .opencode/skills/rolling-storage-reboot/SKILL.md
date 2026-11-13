---
name: rolling-storage-reboot
description: Manually roll a reboot across the storage nodes (new kernel, firmware, microceph snap) one node at a time, proving each comes back healthy, with the storage leader rebooted last. Use for any change that needs a storage host to restart.
---

# Rolling storage reboot

Manual procedure, one node per step. There is no playbook yet; this is the
order of operations that keeps etcd, Ceph, Tang/LUKS and the leader-gated app
stack alive throughout.

## Hard rules

- Rebooting prod needs the user's explicit go-ahead for the window. Then
  reboot **one node at a time** and finish verifying it before touching the
  next one.
- **The storage leader boots last.** Everything leader-gated (PostgreSQL,
  Qdrant, docker registry, nginx/frpc, the app stack) lives there.
- **Never two quorum members down at once.** etcd needs 2 of 3, the Ceph mons
  need 2 of 3, and `/rbd/user` unlocks via clevis/Tang (2-of-3, tang servers =
  the etcd group on port 8777). Rebooting a second etcd/mon/tang node before
  the first is back blocks the LUKS unlock of the next node that needs to
  mount the volume.
- Reboot the **least-roled node first** and the leader last. Typical order:
  a node with no mon/etcd/VIP, then the mon leader/mgr active node, then the
  other quorum members, then the leader.

## Preflight (read-only)

Resolve roles from live state, never from a remembered topology — leader,
mgr and VIP holders all move.

```text
run(command="ycluster cluster leader", hostname="localhost")
run(command="ycluster cluster status", hostname="localhost")     # etcd, DHCP leader, VIPs, leader services
run(command="sudo ceph -s; sudo ceph osd tree; sudo ceph pg stat; sudo ceph mon stat; sudo ceph mgr stat", hostname="<any storage node>")
```

Per node, before the window:

```text
run(command="uname -r; ls -1 /boot/vmlinuz-*; grep -m1 -E '^[[:space:]]*linux[[:space:]]' /boot/grub/grub.cfg; sudo grub-editenv list; sudo needrestart -b | grep -E 'KSTA|KEXP'; df -h /boot; sudo docker ps --format '{{.Names}}|{{.Status}}'; systemctl --failed --no-legend; mount | grep /rbd/ || echo none; sudo rbd showmapped", hostname="<node>")
```

- `NEEDRESTART-KSTA: 1` means no pending kernel; `>=2` means a reboot is due
  (`StorageKernelRebootPending` in `docs/operations/packages.md`).
- Confirm the **target kernel** per node from `/boot/vmlinuz-*` and the first
  `linux` line of `grub.cfg` (default entry). Kernel series can legitimately
  differ per host (GA vs HWE) — do not assume one version fleet-wide, and do
  not proceed if the default entry is not the kernel you intend to boot.
- Workload that must survive: Docker containers with `restart=unless-stopped`
  and `docker` enabled come back by themselves. **Incus VMs do not** — if the
  node hosts VMs, stop or migrate them first and get explicit approval.
- Require `ceph -s` HEALTH_OK and all PGs `active+clean` before starting, and
  again before each subsequent reboot.

## Set the maintenance flag

```text
run(command="sudo ceph osd set noout", hostname="<storage node>")
```

Leave `noout` set for the whole window so a slow boot cannot trigger an
OSD-out and a backfill across the surviving OSDs. While it is set, `ceph -s`
and `ycluster cluster status` show `HEALTH_WARN noout flag(s) set` ("ceph:
degraded") — that is the flag, not data loss.

## Per-node reboot

1. Capture pre-state (kernel, uptime, container list, `systemctl --failed`).
2. Reboot; the SSH session dies with it, so a following call failing with
   `exit=255` / `Connection closed` is expected, not a failure:

   ```text
   run(command="sudo shutdown -r now", hostname="<node>")
   ```

3. Poll the node until it answers (`run(command="uname -r", hostname="<node>")`).
   Do not treat "reachable" as "back": require the **new** kernel.
4. Verify: containers restored, `systemctl --failed` empty, no `/rbd/` mounts
   (non-leader), OSD `up`/`in`, PGs `active+clean`, `NEEDRESTART-KSTA: 1`.
5. Refresh the pending-restart metric so the alert clears immediately instead
   of on the next 15-minute tick — the boot-time sample can still read `1`
   even when the new kernel is already running:

   ```text
   run(command="sudo /usr/local/lib/cluster/apt-metrics-collector; grep -E '^ycluster_restart_kernel_pending' /var/lib/prometheus/node-exporter/apt_status.prom", hostname="<node>")
   ```

## Leader handover (the last node)

1. `run(command="ycluster cluster drain <leader>", hostname="localhost")` — the
   election loop notices within ~10s, stops renewing its 30s lease and tears the
   leader services down, so a new leader appears within roughly 40s.
2. Confirm `ycluster cluster leader` names a **different** node, then verify on
   that node: `/rbd/user` + `/rbd/misc` mounted, `postgresql@16-main`, `qdrant`,
   `docker-registry`, `frpc`, `prometheus-central` active, and the app
   containers up (`forgejo`, `authentik-*`, `vikunja`, `zulip-*`, `open-webui*`,
   `wrangler*`). Probe the vhosts locally on the leader
   (`curl -sk -o /dev/null -w '%{http_code}' --resolve <vhost>:443:127.0.0.2 https://<vhost>/`,
   expect 200) — never hairpin through the public endpoint to test.
3. Only then reboot the old leader, and verify it returns as a clean
   non-leader: no RBD mappings, no `/rbd/` mounts, leader units inactive, and
   only the per-node containers (monitoring) running.
4. `run(command="ycluster cluster undrain <leader>", hostname="localhost")`.
   Leave leadership where it landed — forcing it back costs another teardown
   for no benefit.

The step-down is known to take the aggressive path
(`todo/operations/storage-leader-graceful-teardown.md`): expect `forcing
aggressive cleanup`, `pkill -9 postgres`, force unmount/unmap. Confirm the old
instance actually exited cleanly by checking the new leader's log for
`database system was shut down at` with no redo/recovery block:

```text
run(command="sudo grep -E 'database system was (shut down|interrupted)|redo starts at|redo done|ready to accept connections' /var/log/postgresql/postgresql-16-main.log", hostname="<new leader>", tail_lines=6)
```

A clean handover shows `database system was shut down at …` followed by
`ready to accept connections`, with no `redo starts at` in between.

## Close out

```text
run(command="sudo ceph osd unset noout", hostname="<storage node>")
run(command="sudo ceph -s; sudo ceph pg stat; sudo ceph osd dump | grep -E '^flags'", hostname="<storage node>")
```

Unsetting `noout` right as a node rejoins causes a short out/in and a few
MiB/s of backfill (`active+undersized+degraded` for seconds) before settling to
HEALTH_OK — re-read `ceph pg stat` until every PG is `active+clean` rather than
calling it at the first sample.

If the window is interrupted, `ycluster cluster undrain <node>` every node you
drained before walking away: the drain flag lives in etcd and survives a reboot
of that node, leaving it permanently ineligible for leadership.

End state to report, per node and cluster-wide:

- every storage node on the intended kernel, `NEEDRESTART-KSTA: 1`, zero failed
  units, expected containers;
- `ycluster cluster status`: etcd 3/3, single DHCP leader, both VIPs placed,
  leader services healthy;
- Ceph HEALTH_OK, all OSDs up/in, all PGs active+clean, no `noout` in flags.

## Gotchas

- `systemctl is-active` lies for the RBD oneshot units (`user-rbd`,
  `misc-rbd`): they can read `active` on a node with nothing mounted (stale
  state from an earlier leadership change). Use `mountpoint -q /rbd/user` /
  `mount | grep /rbd/` as the source of truth for leadership and mounts.
- A leader failover can leave a transient `vm-idle-alert.service` failure
  (central Prometheus restarting at that moment). Check
  `systemctl is-active prometheus-central` and re-run the unit; don't report it
  as a new problem unless it fails again with Prometheus healthy.
- cluster-exec `run()` rejects `head`/`tail` (except `-1`) and sleeps over 10s
  in the command string; use the `head_lines`/`tail_lines` arguments and poll
  with short commands instead.
- Keep the disk-pressure check in mind for `/boot` before rebooting
  (`df -h /boot`): a full `/boot` with a new kernel installed is how a reboot
  turns into a recovery.
