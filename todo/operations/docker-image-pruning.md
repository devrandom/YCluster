# Periodic Docker image pruning on storage nodes

Dangling image layers from open-webui rebuilds accumulate in
`/var/lib/docker/overlay2` and fill the root fs (the mon store lives on
`/`, `/dev/mapper/vg0-root`), tripping `MON_DISK_LOW` (incident:
`../ycluster-private/incidents/2026-06-04 — s2 MON_DISK_LOW from docker
layers`).

Schedule a periodic `docker image prune -f` (dangling-only — never `-a`,
which on a non-leader would delete the tagged-but-not-running open-webui
`:latest` and force a re-pull on failover) via a systemd timer or
Ansible-managed cron on each storage node.

Note s2/s3 root disks are smaller than s1 so they hit the 30%
`mon_data_avail_warn` threshold sooner. Registry blobs are unaffected
by pruning — they live on RBD at `/rbd/misc/docker-registry/data`,
outside Docker's image store.
