# Ceph off-cluster backup

RBD (`rbd/user`/`rbd/misc`) has no off-cluster copy. The DB dumps cover
the stateful services; the remaining at-risk content is the docker
registry blobs (`/rbd/misc/docker-registry`) and any hand-placed files.

## Plan

Implement `rbd export-diff` → age-encrypt → existing offsite destinations,
scheduled with `backup-databases`.

Refs:
- `docs/operations/disaster-recovery.md#ceph-off-cluster-backup-story`
  (design + scope)
