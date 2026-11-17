# Ship cluster logs off-host

Every log the cluster keeps is local to the node that wrote it. A disk failure,
a dead node, or a host-root attacker takes it with them, and there is no way to
search across hosts during an incident. This matters most for the IDS record:
under the delivery model, a finding that does not page exists **only** in
`/var/lib/ids-forwarder/detections.jsonl`, so the file is the entire history of
everything the sensor decided not to tell anyone about.

Measured today for scale:

- `detections.jsonl` is 45 MB on the busiest core node and 12 MB on a compute
  node, against a 50 MB rotation threshold and `keep: 10`. The busy node is
  already within one day of rotating, which is why
  `IdsForwarderRecordRotating` exists.
- `/var/log/journal` is 4 GB per host at the current `SystemMaxUse`, so
  shipping it wholesale is not the first increment.
- nginx access logs on the core node are ~20 MB, squid ~11 MB.

## Work

- **Ship the IDS record first.** It is the smallest, highest-value, most
  regularly sized stream. Options: rotation-aware `rsync --append` to the
  storage leader on a timer, a small tailer that POSTs complete lines to an
  ingest endpoint, or journald-native forwarding if the record is also written
  to the journal (it is not today). Whichever is chosen, ship the rotation
  offset so a restart resumes instead of duplicating, and alert when the
  leader has not received a host's data for longer than the collector interval.
- **Decide the store.** Leader-local directory is the cheap first step and
  inherits the leader's failure domain; CephFS or an object store gives
  durability at the cost of a new dependency. Indexing (Loki) is a separate
  question: grep over tens of MB per day does not need it, cross-host
  correlation during an incident does.
- **Then journald, filtered.** Auth, unit failures and CrowdSec's own log are
  the useful subset; a `journald-remote` or rsyslog filter keeps the volume
  near the IDS record's rather than the full 4 GB.
- **Keep tetragon's retained event log out of scope initially.** It is the
  upstream buffer the forwarder replays from, not a review artifact.
- **Privacy.** The IDS record carries `actor`, `uid`, container names and
  origin, so the destination stays inside the cluster: no external log service,
  and the same sanitization rule applies as to anything else in this repo.

## Decisions needed

- Where the store lives (leader disk, CephFS, object store) and how long it is
  retained, given the 10-rotation window on the host today.
- Whether cross-host search is a requirement in the first increment or a
  later one; it decides between plain files and an index.
- Whether to accept a per-host tailer (a new small service) or stay with timers
  and `rsync`.

## Related

- `docs/design/ids-coverage-and-evidence.md` — the record's role as the only
  copy of a non-paging finding, and the queries an incident review runs.
- `todo/monitoring/alternative-alert-paths.md` — the same single-path problem
  on the notification side.
