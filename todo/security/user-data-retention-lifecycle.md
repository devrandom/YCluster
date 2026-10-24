# User-data retention lifecycle

There is no defined lifecycle for user data. Each service keeps its own data
with whatever default its upstream ships, backups age out on a separate schedule,
and deleting an account or a chat does not have a documented path through the
caches, search indexes, logs, and backups that also hold a copy. This item
inventories the stores, decides the retention and deletion semantics, and makes
deletion verifiable.

## Where user data lives

Verify each entry before treating it as authoritative; this is the starting
map, not an audit result.

- **Open WebUI.** Chats, prompts, uploads and user settings in PostgreSQL, plus
  the cache under `/rbd/misc/app/open-webui-data`. Confirm whether the vector
  store holds embeddings derived from chat content.
- **Authentik.** User records, groups, sessions and proxy sessions in PostgreSQL;
  media and certificates under `/rbd/misc/authentik`.
- **ComfyUI.** Outputs, uploads and per-user workflows on the NAS under
  `/near1`.
- **MinerU.** Uploaded documents and parse results; task retention is currently
  24h, but the result files and any user-visible copies need their own policy.
- **Forgejo.** Repositories, issues, attachments, avatars, Actions logs and
  artifacts; PostgreSQL metadata plus RBD content. The database backup exists, but
  there is no `forgejo dump` path yet.
- **Inference gateway.** Per-user usage records (tokens, model, timestamps) and
  per-user API keys in etcd. Confirm whether prompt or completion content is
  logged anywhere.
- **VMs.** User-owned disks and any snapshots or backups of them. Confirm whether
  VM data is in scope for this policy or explicitly delegated to the VM owner.
- **Backups.** Daily PostgreSQL, Qdrant and etcd dumps with an offsite copy and a
  30-day retention. A deleted row can therefore survive in backups for up to the
  backup window.
- **Logs and metrics.** nginx/SSH access logs, journald, and Prometheus (90d
  central retention). These may contain user IDs, IPs, paths or request metadata
  even when the primary content is deleted.

## Decisions needed

- **Data classes.** Separate account data, user content, derived data
  (embeddings, indexes, thumbnails), usage metadata, logs, and backups. Each
  class needs its own retention rule; one global "keep everything" or "delete
  everything" rule will not work.
- **Retention periods.** Define how long each class is kept while active, after
  deletion, and after account closure. State the reason for each period rather
  than inheriting an upstream default.
- **Deletion semantics.** Decide hard delete vs soft delete, what a user can
  delete themselves, what requires an admin, and what happens to shared or
  derived content when the source is deleted.
- **Backups.** Decide whether deletion propagates into backups (tombstones or
  scrubbing) or whether backups simply age out. Document the maximum time a
  deleted item can reappear after a restore, and make restores re-apply
  deletions rather than silently resurrecting them.
- **Caches and indexes.** Define eviction and rebuild rules for the Open WebUI
  cache, vector store, search indexes, and thumbnails. Deleting the primary row
  must not leave a searchable copy behind.
- **Inactive accounts.** Decide whether inactive accounts are disabled,
  archived, or deleted, and after how long.
- **Ownership and access.** Name who can approve an export or deletion, who can
  retrieve data during an incident, and how that access is logged.

## Work

- Produce the store inventory above with schema/table or path, owner, current
  retention, deletion path, and whether it is covered by backups.
- Write a retention table per data class, with the deletion trigger and the
  verification method.
- Implement deletion propagation for account deletion and user-initiated content
  deletion: primary store, object store, cache, vector index, search index,
  sessions/proxy sessions, API keys, and logs where feasible.
- Define the backup story explicitly: either scrub deleted records or document the
  aging-out window and require restores to replay deletions.
- Add a periodic audit that reports orphaned data (rows or files with no owning
  user), stores with no retention policy, and backup copies older than the stated
  window.
- Add a deletion drill: create a test user, generate data in each service, delete
  the user, and verify the data is gone from primary stores, caches, indexes and
  (after the documented window) backups.
- Add metrics/alerts for deletion failures, audit backlog, and stores that exceed
  their retention window.
- Decide whether usage records and user IDs in Prometheus labels are compatible
  with the retention policy; label cardinality and retention are both long-lived.

## Open questions

- Which services and data classes are in scope: are VM disks and ComfyUI
  outputs user data, or owner-managed data outside this policy?
- Is there a contractual or legal retention requirement, and does it differ per
  tenant or project?
- Should a user be able to export their data, and in what format?
- What is the deletion SLA, and what evidence is returned to the requester?
- Do backups need scrubbing, or is aging out plus restore-time deletion
  sufficient?

Related: [`intrusion-detection.md`](intrusion-detection.md) (IP retention),
[`audit-log-story.md`](audit-log-story.md) (log retention),
[`../lifecycle/ceph-off-cluster-backup.md`](../lifecycle/ceph-off-cluster-backup.md)
(off-cluster copies), and
[`../lifecycle/test-restore-job.md`](../lifecycle/test-restore-job.md)
(restore verification).
