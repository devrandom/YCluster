# osv-feed

Leak-free, stdlib-only OSV vulnerability feed: mirror OSV ecosystem data
to a local directory and match installed package versions against it,
without ever sending your package inventory or versions to a remote
service.

The facility is self-contained on purpose: it is expected to move to its
own repository and only depends on the Python standard library.

## Example

`match` triages installed package versions against the mirrored feed and
labels each finding with its tracking category, severity, state, and any
acknowledgment:

```bash
$ osv-feed match --ecosystem Debian
ECOSYSTEM  CATEGORY   PACKAGE  VERSION             SEVERITY  CVE                    STATE       ACK
---------  ---------  -------  ------------------  --------  ---------------------  ----------  --------------------
Debian     perimeter  nginx    1.26.3-3+deb13u7    high      DEBIAN-CVE-2026-60005  no_fix_yet
Debian     perimeter  openssh  1:10.0p1-7+deb13u4  critical  DEBIAN-CVE-2026-60002  no_fix_yet
Debian     network    dnsmasq  2.91-1+deb13u1      medium    DEBIAN-CVE-2026-12725  no_fix_yet
Debian     user       sudo     1.9.16p2-3+deb13u2  high      DEBIAN-CVE-2023-22809  patched     2026-08-15 14:30 UTC
```

Adding `--verbose`/`-v` appends each CVE's title and important attributes
under its row, and `--format json`/`--format prometheus` emit machine-readable
output instead of the table.

## Why

- OSV's query API asks you to POST `(package, version)` pairs. That leaks
  exactly what a perimeter defender does not want to share.
- OSV publishes full ecosystem archives plus `modified_id.csv` incremental
  indexes on GCS. Pulling those is a one-way public-data download; every
  subscriber downloads the same files, so package interest is not revealed
  either.
- The per-record OSV format includes ranges with `introduced`/`fixed`
  events, so a local mirror still supports the full
  `patched` / `fix_available` / `no_fix_yet` classification.
- We want to have policies and alerting

## Install

Install the `osv-feed` console command with uv (no runtime dependencies are
pulled in):

```bash
uv tool install ./osv-feed
```

Once the facility is published, `uv tool install git+https://...` will work
unchanged against the same pyproject.

## Layout and configuration

Local data lives under a DB directory, configurable via the CLI flag, the
`OSV_FEED_DB_DIR` environment variable, or TOML config files:

```text
<db_dir>/osv-feed.sqlite           single shared SQLite store (all ecosystems)
<db_dir>/<ecosystem>.cursor        last incremental sync timestamp
<db_dir>/epss.json                 mirrored EPSS snapshot (score + percentile per CVE)
<db_dir>/epss.cursor               latest EPSS mirror timestamp
<db_dir>/first-seen.json           first-seen state for prometheus output
```

All mirrored ecosystems live in one SQLite database
(`<db_dir>/osv-feed.sqlite`). The database uses rollback-journal (DELETE)
journaling, so the single `.sqlite` file is always self-consistent once the
writer closes — a plain copy of that file after a `mirror` completes is a
valid snapshot.

Default DB directory: `~/.cache/osv-feed`. CVE acknowledgments are kept in
a separate, configurable JSON store (`[feed] ack_db`, default
`~/.cache/osv-feed-acks.json`) so it can live on shared storage instead of in the
mirrored feed data.

### Config files

TOML configuration is read from, in increasing precedence:

1. `/etc/osv-feed/config.toml` (system)
2. `~/.config/osv-feed.toml` (per-user, overrides per key)

`OSV_FEED_DB_DIR` / `OSV_FEED_ACK_DB` / `OSV_FEED_STATE_DIR` override both
config files, and the CLI flags override everything. Example:

```toml
[feed]
db_dir = "/srv/osv-feed/db"
ack_db = "/srv/osv-feed/acks.json"
state_dir = "/srv/osv-feed/state"
immutable_db = false

[tracking.perimeter]
ecosystem = "Debian"
packages = ["openssh-client", "nginx", "keepalived"]
severity = "high"
states = ["no_fix_yet", "fix_available"]
interests = ["rce", "remote code execution"]

[tracking.user]
packages = ["openssh-client", "sudo"]
severity = "high"
interests = ["privilege escalation", "local privilege"]
```

A tracking category groups packages and a local filter policy for one
matching run. `--category <name>` resolves `[tracking.<name>]` and applies:

- `ecosystem` — default ecosystem for the category's packages;
- `packages` — source/binary package names, optionally `name=version`;
- `severity` — minimum severity (`low`/`medium`/`high`/`critical`);
- `states` — allowed CVE states (`patched`, `fix_available`,
  `no_fix_yet`);
- `interests` — case-insensitive substrings matched against OSV
  `summary`/`details`, so e.g. a perimeter category can focus on
  remotely exploitable issues.

Self-built Go binaries (e.g. `frp`) are tracked the same way against the
`Go` ecosystem, pinned to the version we build via the `name=version`
package form (`ecosystem = "Go"`, `packages = ["github.com/fatedier/frp=0.70.0"]`),
so no dpkg resolution is needed on the collector.

### Built-in default categories

A few category names are built in and work without any config. Defining a
`[tracking.<name>]` table for one of these replaces the built-in default
entirely, so you can tighten or relax it to taste:

| name | packages | policy |
| --- | --- | --- |
| `perimeter` | `openssh`, `nginx` | severity `high` |
| `user` | `sudo` | severity `high`, interests `privilege escalation`/`local privilege` |
| `network` | `dnsmasq`, `squid`, `keepalived`, `wireguard-tools` | severity `medium` |
| `runtimes` | `docker.io`, `containerd`, `runc`, `podman` | severity `high` |
| `container` | — (driven by `container/*.manifest` files) | severity `high` |

The built-ins use source package names (for example `openssh` covers the
installed `openssh-client`), and all default categories allow the
`no_fix_yet` and `fix_available` states.

### Match category defaults

When you run `match` without `--category`, the packages are auto-categorized:
each package is matched against every category whose package set contains it,
and the findings are labeled with those categories and filtered by each
category's policy. A package that belongs to no category falls back to a
single `default` category with a **medium** minimum-severity policy
(`no_fix_yet` / `fix_available` states), so an unknown package is still
triaged rather than passed through unfiltered. Passing `--category <name>`
forces exactly one category and keeps its behavior unchanged.

Running `match` with no `--pkg`, `--spec-file`, or `--category` matches
every package listed in any tracking category (built-in and configured), so
`python3 -m osv_feed match --format prometheus` reports the whole tracked
surface.

For tests/packaging the config paths can be redirected with
`OSV_FEED_SYSTEM_CONFIG` and `OSV_FEED_USER_CONFIG`.

## Usage

Mirror one or more ecosystems (first run downloads the full archive, later
runs use `modified_id.csv` for changes):

```bash
python3 -m osv_feed --db-dir /srv/osv-feed/db mirror --ecosystem Ubuntu
python3 -m osv_feed mirror --ecosystem Ubuntu --ecosystem Debian --ecosystem Go
python3 -m osv_feed mirror --ecosystem Go   # self-built Go binaries (e.g. frp)
python3 -m osv_feed mirror --ecosystem PyPI --ecosystem npm  # container images
```

When `--ecosystem` is omitted on `mirror` or `match`, it defaults to the
host OS via `/etc/os-release` when that is Debian or Ubuntu. On any other
distribution there is no default and `--ecosystem` is required.

Mirror writes progress to stderr by default (full-archive start/bytes/
extracted-count, and per-record `updated`/`removed` lines on incremental
sync). Pass `--quiet` (or `-q`) to suppress it for cron/scripts.

Each `mirror` run also pulls the daily EPSS snapshot (`epss_scores-current`
from the EPSS API) into `<db_dir>/epss.json`, so every finding can be
enriched with an exploit-probability score and percentile without any
outbound request at `match` time. When a fresh snapshot is already present
it is skipped (re-download with `--force-full`); `match` still works without
it, reporting EPSS as `0` for every CVE.

Match package versions locally:

```bash
python3 -m osv_feed match --pkg 'openssh-client' --format table
python3 -m osv_feed match --pkg 'openssh=1:9.6p1-3ubuntu13.3' --format json
python3 -m osv_feed match --pkg 'nginx' --format prometheus
python3 -m osv_feed match --category perimeter --format prometheus
```

When `=VERSION` is omitted, the installed version is resolved locally from
`dpkg-query`, and binary package names are mapped back to their source
package (for example `openssh-client` → `openssh`). Distro CVEs are tracked
under source package names, so this makes `--pkg openssh-client` match
without hardcoding a version.

Mixed ecosystems use a JSON spec file:

```bash
python3 -m osv_feed match --spec-file specs.json --format prometheus
```

`specs.json` is an object or array of `{"ecosystem", "package", "version"}`.
`version` is optional and defaults to the installed version.

### Container images

Container images whose CVE surface you want to track are described by
plain-text manifests, one `ecosystem:name=version` spec per line (`#` starts
a comment). Each manifest is a regular file named `<image>.manifest` in a
container directory, and every spec is matched against the local mirror:

```bash
# webapp 0.11.0 (source SHA 0123abc...)
PyPI:requests=2.32.3
npm:express=4.19.2
Go:github.com/example/lib=1.2.3
Ubuntu:libssl3=3.0.13-1ubuntu0.1
```

```bash
python3 -m osv_feed match --container-dir /srv/osv-feed/manifests --format prometheus
```

Findings are tagged `category=container` and filtered through the
`[tracking.container]` table (built-in default: severity `high`, pending
states), and each finding carries an `image` label from its manifest file
name.

Manifests are generated deterministically from a pinned build tree via
`osv_feed.manifestgen` (stdlib-only: parses `uv.lock` for the `PyPI`
surface and `package-lock.json` for `npm`). Since a `uv.lock` often also
pins dev tooling that is never shipped in the runtime image (black,
pytest, ruff, daphne, mypy…), `--uv-select` restricts the emitted surface:
`all` (every installable lockfile package), `base` (the root project's
production dependencies), or a named group such as `prod` (closes over that
dependency group), so the manifest matches the runtime surface the image
actually ships. `npm` is typically excluded when the SPA frontend is baked
at build rather than shipped in the runtime image:

```bash
python3 -m osv_feed.manifestgen webapp \
  --uv path/to/uv.lock --uv-select base \
  --comment "webapp v0.11.0 0123abc" \
  --out webapp.manifest
```

First-seen and alerting are per-image, so the same transitive Python
package in two images is tracked independently. `--container-dir` is
additive — combine it with the default no-flag run (all tracked host
category packages) to cover host + container CVEs in one collector pass, and
mirror the ecosystems the images need (e.g. `PyPI`, `npm`) alongside the
host ecosystem.

When `--container-dir` is omitted, `OSV_FEED_CONTAINER_DIR` (a path-list
separated by `:`) is honored, so the same collector/systemd wiring that
drives `db_dir` via `OSV_FEED_DB_DIR` can point the textfile collector at
the published manifest dir. A missing/empty dir yields no container
findings.

`match` warns on stderr when a requested ecosystem feed is absent or its last
sync is more than a day old, since results would be stale or empty until you
run `osv-feed mirror --ecosystem <name>`. An absent feed exits non-zero;
a stale feed still proceeds.

`--verbose` (or `-v`) with `--format table` prints extra detail under each
row: the CVE title (OSV `summary`), the category `interests` that matched
(e.g. `rce`, `privilege escalation`), any alias IDs, and the advisory
reference URLs. The same fields (`summary`, `details`, `aliases`,
`references`, `published`, `modified`) are always present in `--format json`
output, so tooling can consume them without a verbose flag.

### Acknowledgments

Acknowledged CVEs stay visible in `match` output so the audit trail is
preserved, but they are labeled (`acked="true"` in prometheus output) so
alerting rules can suppress them. Acknowledgments are keyed per
`category + ecosystem + cve` and stored in the ack DB (`--ack-db`,
`[feed] ack_db`, or `OSV_FEED_ACK_DB`):

```bash
python3 -m osv_feed --ack-db /srv/osv-feed/acks.json \
  ack add --category perimeter --ecosystem Debian \
  --cve DEBIAN-CVE-2026-60000 --note 'blocked at firewall'
python3 -m osv_feed --ack-db /srv/osv-feed/acks.json \
  ack list --category perimeter
python3 -m osv_feed --ack-db /srv/osv-feed/acks.json \
  ack remove --category perimeter --ecosystem Debian \
  --cve DEBIAN-CVE-2026-60000
```

`ack list` supports `--format table` (default) or `--format json`.

The prometheus format emits:

- `osv_feed_cve_info{ecosystem,package,version,cve,severity,state,category,acked}`
  — `1` per finding, state is `patched`, `fix_available`, or `no_fix_yet`.
  `severity` is the distro label when present (Debian urgency, Ubuntu
  priority) or a CVSS 3.x qualitative rating
  (`low`/`medium`/`high`/`critical`); CVSS 4.0 / GHSA records use the
  advisory's own `database_specific.severity` label. It may be empty when
  the record has no usable severity. `category` is the tracking category
  name (empty for uncategorized matches) and `acked` is `true`/`false`.
  Container findings add an `image="<name>"` label from their manifest.
- `osv_feed_cve_first_seen_seconds{...}` — persisted first-seen epoch so
  alerts can fire on "pending longer than N days".
- `osv_feed_cve_summary{severity,acked,cves}` — one series per alert
  severity (`critical`/`high`), value = count of non-acked pending findings
  past that severity's SLA, `cves` = up to 5 example CVE IDs (oldest
  first-seen first). Emitted only when the count is non-zero. This is what
  coalesced perimeter-CVE alerts fire on, replacing per-CVE pages.
- `osv_feed_cve_epss_score{ecosystem,package,version,cve}` and
  `osv_feed_cve_epss_percentile{...}` — EPSS exploit-probability score and
  percentile (0..1) for the CVE, `0` when the snapshot is absent or the CVE
  is not in it.

## Matching semantics

- Package names are matched case-insensitively against each OSV record's
  `affected[].package.name`.
- OSV ranges are evaluated with the standard `introduced` / `fixed` /
  `last_affected` semantics, plus exact `versions` lists when present.
- `fix_available` means the affected range names a fixed version and the
  installed version is below it; `no_fix_yet` means the installed version is
  affected but no fixed version is recorded.
- Debian-family version comparison uses `dpkg --compare-versions` when
  dpkg is installed, with a best-effort numeric-run fallback key otherwise.
- Ubuntu/Debian CVEs are tracked against **source package** names. When you
  supply a version explicitly, pass the source package name (for example
  `openssh`, not `openssh-client`) or the record will not match; when you
  omit the version, binary package names are mapped to their source package
  automatically.

## Mirror mechanics

Initial sync downloads `<ecosystem>/all.zip` (for example Ubuntu is
roughly 590 MB, Debian roughly 69 MB, Go roughly 11 MB). Daily sync
downloads the small `<ecosystem>/modified_id.csv` (about 3 MB for Ubuntu),
fetches only records newer than the saved cursor, and removes local records
whose upstream file returns 404 (withdrawn). No host data is sent at any
point.

All ecosystems share the single `osv-feed.sqlite` store, and each record
also fills a `packages` table so `match` loads only records that mention
the requested packages instead of scanning the whole archive.

### Publishing a read-only snapshot

The writable DB lives on a node with local storage for reasons of
robustness: SQLite over a network filesystem (e.g. on a Samba/NFS share)
invites locking and journaling problems. A separate deployment job copies
`<db_dir>/osv-feed.sqlite` to a shared NFS directory (for example
`/mnt/near1-osv-feed`) after each `mirror` run, using an atomic replace so
consumers always see a complete file.
`osv-feed` itself only maintains the writable store; the publish step is
owned by the surrounding repo's systemd job.

`match` reads that snapshot with `--db-immutable`, which opens the file
with `mode=ro&immutable=1` so SQLite takes no locks and never writes
journal/`-wal`/`-shm` files to the share. The surrounding publish job
copies the EPSS snapshot and the per-ecosystem cursors alongside the DB
(a reader's `match` run consults them), but `first-seen.json` is never
written into the read-only snapshot dir: under `--db-immutable` the
writable `first-seen.json` falls back to the local default DB dir
(`~/.cache/osv-feed`), so consumers pointed at that snapshot never
touch the writable store.

`immutable_db=true` can also be set via `OSV_FEED_IMMUTABLE_DB` or
`[feed] immutable_db`; the CLI flag wins over both. The first-seen state
directory can be pinned explicitly with `--state-dir`,
`OSV_FEED_STATE_DIR`, or `[feed] state_dir` when the default location is
not appropriate.

## Tests

```bash
./test.sh
# or, from this directory:
PYTHONPATH=. python3 -m unittest discover -s tests
```

## Status

Repository-local facility, generic and self-contained by design. Deployment (a mirror on a collector host plus a textfile collector
and CVE alerts) and the migration to a standalone repository are
follow-ups.
