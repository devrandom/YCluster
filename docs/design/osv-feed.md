# Leak-free OSV CVE feed (`osv-feed/`)

A repository-local, self-contained facility for checking CVEs against
installed packages without uploading the host's package inventory or
versions. It is designed to move to its own repository unchanged; see
`osv-feed/README.md` for the standalone user manual and commands.

## Why this shape

- The OSV query API wants `(package, version)` pairs POSTed. For perimeter
  hosts that leaks the exact software the boundary runs.
- OSV publishes full ecosystem archives and per-ecosystem
  `modified_id.csv` incremental indexes on public GCS. The mirror pulls
  those same files every subscriber pulls, so neither versions nor package
  interest leaves the network.
- The local records retain the OSV `introduced`/`fixed`/`last_affected`
  ranges, which is enough to classify each CVE for an installed version as:
  - `patched` — version is outside the affected range (or the package is
    not mentioned),
  - `fix_available` — affected and a fixed version is recorded,
  - `no_fix_yet` — affected and no fixed version is recorded yet.

## Facility contents

- `osv-feed/mirror.py` — initial full download of `<ecosystem>/all.zip`
  and incremental sync via `<ecosystem>/modified_id.csv`; withdrawn records
  (404) are removed locally. All ecosystems are held in one SQLite database
  (`<db_dir>/osv-feed.sqlite`, rollback-journal mode so the single file is
  always a valid snapshot), under a configurable DB directory
  (default `~/.cache/osv-feed`; TOML configs at `/etc/osv-feed/config.toml`
  and `~/.config/osv-feed.toml`, env `OSV_FEED_DB_DIR`, or `--db-dir`).
  An in-database `packages` table lets `match` load only relevant records.
- `osv-feed/matching.py` — OSV range evaluation, dpkg-native version
  comparison with a fallback key, and the three-state classification.
- `osv-feed/config.py` — merges system + user TOML into one full config;
  resolves the feed DB dir, the ack DB path, the read-only snapshot
  (`immutable_db`), and `[tracking.<name>]` category tables (user tables
  override the built-in defaults).
- `osv-feed/categories.py` — built-in default categories (`perimeter`,
  `user`, `network`, `runtimes`) plus `[tracking.<name>]` package sets and
  policy filters (minimum severity, allowed states, summary/details
  interests). A config category replaces the default of the same name; a
  medium-policy `default` fallback triages packages that belong to no
  category.
- `osv-feed/acks.py` — atomic JSON acknowledgment store (keyed by
  `category + ecosystem + cve`, plus an optional `surface`) so acknowledged
  CVEs stay visible but are labeled for alert suppression. A `surface` is an
  arbitrary deployment target (a container image, or a non-container install
  such as `local-ai-proxy`); an empty surface is a wildcard that keeps the
  legacy three-part key valid, and surface-scoped acks only suppress the CVE
  on that surface.
- `osv-feed/cli.py` — `mirror`, `match`, and `ack` commands; `match`
  emits `table`/`json`/`prometheus` output and accepts `--category` for a
  configured tracking set. Without `--category`, `match` auto-tags each
  package against every category that lists it, falling back to the
  medium-policy `default` category for unmatched packages. With no
  `--pkg`/`--spec-file`/`--category` it matches every package in any
  tracking category, so a bare run reports the whole tracked surface.
  `--verbose`/`-v` enriches the table with the CVE title, matched category
  `interests`, aliases and reference URLs; json output always carries
  `summary`/`details`/`aliases`/`references`/`published`/`modified`.
  Prometheus output includes
  `osv_feed_cve_info` with `category`/`acked` labels and
  `osv_feed_cve_first_seen_seconds` so alerts can fire when a CVE has been
  pending longer than a configured window.
- `osv-feed/tests/` — server-free unittest suite (full mirror and
  incremental sync tests use a local HTTP bucket fixture).

Sizes (compressed, measured 2026-08-15): Ubuntu `all.zip` ~590 MB,
Debian ~69 MB, PyPI ~32 MB, Go ~11 MB; Ubuntu `modified_id.csv` ~3 MB
per day.

## YCluster wiring (repo-landed)

The mirror and the CVE textfile collector are wired into the monitoring
stack repo-side; deployment to the cluster is `monitoring.yml`, which
imports `install-osv-feed.yml`. The playbook installs the package source
and wrapper plus a single-node collector (default `core[0]`, override
with `-e osv_feed_collector=...`), the daily mirror service/timer, the
`/etc/osv-feed/config.toml` tracking categories, the `ycluster-security`
Prometheus alert group, and the daily-repeat Alertmanager perimeter route.
Only the playbook still needs to actually run against the cluster.

The design that drove it:

Run the mirror and CVE matching on a single designated node that has
persistent storage for the feed DB and outbound access to
`storage.googleapis.com/osv-vulnerabilities`. A single writer on that DB
avoids split-brain on the daily mirror and on the `first-seen.json` gauge.

1. **Ship the tool (no prebuilt binaries).** Install the package source to
   a system path and expose an `osv-feed` wrapper (`exec python3 -m
   osv_feed "$@"`), and write `/etc/osv-feed/config.toml` pointing
   `db_dir` and `ack_db` at the chosen storage plus the
   `[tracking.<name>]` category tables (with an explicit `ecosystem` so
   runs are deterministic).

2. **Daily mirror.** An `osv-feed-mirror` `Type=oneshot` service runs
   `osv-feed mirror --ecosystem Debian --ecosystem Ubuntu --ecosystem Go`
   (the Go feed tracks self-built binaries like `frp` that we ship from
   source pins), driven by a
   `Persistent=true` daily timer with a randomized delay. Gate execution
   so only the single writer node runs it. All ecosystems land in the one
   writable `osv-feed.sqlite` on the node's local disk.

3. **Publish a read-only snapshot.** SQLite directly over a network
   filesystem (Samba/NFS) is fragile — locking and journal files on the
   share. Instead, a separate `Type=oneshot` systemd job in the bigger
   repo copies `<db_dir>/osv-feed.sqlite` to `/near1` with an atomic
   replace after each mirror run. Because the DB uses rollback-journal
   (DELETE) mode, the copied single file is always a complete snapshot.
   The publish step also copies the EPSS snapshot and the per-ecosystem
   cursors, which a reader's freshness/`match` run consults; `first-seen.json`
   is deliberately *not* published — it is per-consumer writable state.
   `osv-feed` depends only on the writable store being fresh; it never
   publishes the snapshot itself.

4. **Textfile collectors.** Two outputs into the node_exporter textfile
   directory:
   - per-node perimeter package versions, emitted as version-info metrics
     (`...package_info{pkg,tier,version} 1`) by a local collector that
     maps dpkg binaries to source packages
     (`dpkg-query -W -f '${Package} ${Source}'`);
   - CVE state via `osv-feed --db-immutable match --format prometheus`
     (a bare run reports the whole tracked surface) pointed at the
     `/near1` snapshot, emitting `osv_feed_cve_info` and
     `osv_feed_cve_first_seen_seconds`. `--db-immutable` opens the file
     with `mode=ro&immutable=1` so readers take no locks and never write
     `-journal`/`-wal`/`-shm` files to the share. The first-seen gauge
     needs writable state, so under `--db-immutable` `first-seen.json`
     falls back to the local default DB dir
     (`~/.cache/osv-feed`; pin it with `--state-dir` if needed) and is
     never written into the read-only `/near1` snapshot.

5. **Alert rules.** Add `PerimeterCVECriticalLongPending` /
   `PerimeterCVEHighLongPending` to the Prometheus rules, firing from
   `osv_feed_cve_first_seen_seconds` past the SLA (`for: 1h`), suppressing
   acknowledged CVEs via the `acked="true"` label, and keeping
   Alertmanager `repeat_interval` daily so unresolved `no_fix_yet`
   findings stay visible.

6. **Ownership.** `osv-feed ack add` silences a reviewed finding while
   keeping it visible; keep the apt `upgradable`/USN check as the
   zero-lag `fix_available` cross-check.

## Verification

- `osv-feed/test.sh` stays green.
- Run the mirror once and confirm `<db>/osv-feed.sqlite` plus the
  per-ecosystem cursor; the publish job copies the sqlite file to `/near1`
  and `osv-feed --db-immutable match --format prometheus` pointed at the
  snapshot emits `osv_feed_cve_first_seen_seconds`, scraped by the
  textfile collector.
- `promtool check rules` passes on the new alert group, and a synthetic
  high/critical pending CVE fires after the SLA window.
