# s4: validate as a storage-leader before undraining

s4 is **intentionally drained** — do not undrain until validated as a
storage-leader. s4 participates in Ceph/storage but has never been
exercised as the storage leader (RBD mount of `/rbd/user`+`/rbd/misc`,
postgres/qdrant/registry/rathole takeover, storage-VIP migration), so
it's held out of leader election via `/cluster/nodes/s4/drain=true`.
**This is the correct steady state, not an anomaly.**

## To make s4 leader-eligible

During a maintenance window, drain the current leader so leadership lands
on s4, verify it mounts both RBD volumes cleanly, brings up all
leader-only services, and that the storage VIP + external URL follow;
only then leave it undrained.

## Overlaps

- `../security/core-group-definition.md` ("is s4 core?")
- The node-lifecycle runbook (`../lifecycle/replace-dead-node-runbook.md`)

Same s4 caveat as the gateway-VIP exclusion (now closed, see
`CHANGELOG.md`) — s4 differs from s1-s3 in more than one subsystem.
