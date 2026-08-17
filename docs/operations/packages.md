# Package Management

## Held Packages

Some packages are held from automatic upgrades via `dpkg --set-selections` because
unattended-upgrades would restart their services, which are managed by leader election.
An unexpected restart of PostgreSQL (for example) causes a multi-hour outage since the
leader election script doesn't know to restart it.

Currently held:
- `postgresql-16`
- `postgresql-client-16`

The hold is applied by the `storage/install-postgres.yml` playbook.

## Upgrading Held Packages

Run the upgrade playbook manually when convenient:

```bash
cd /etc/ansible && ./run-playbook.sh storage/upgrade-held-packages.yml
```

This playbook runs `serial: 1` (one node at a time) and will:
1. Unhold packages, upgrade, re-hold
2. Restart PostgreSQL on the storage leader if it was upgraded
3. Record the upgrade timestamp for Prometheus monitoring

A `HeldPackagesStale` warning alert fires if held packages haven't been upgraded
in over 30 days.

## Checking Held Packages

```bash
# On any core node:
dpkg --get-selections | grep hold

# Check available upgrades for held packages:
apt list --upgradable 2>/dev/null | grep -E 'postgresql'
```

## Held Snaps (microceph)

Snaps are held too, and like apt holds they only stay current if someone
upgrades them deliberately. `microceph` is pinned on every storage node via
`snap refresh --hold` (applied by `storage/add-ceph-nodes.yml`) because
microceph refuses cluster operations (e.g. add OSD) when members run
different revisions — a surprise refresh on one node would wedge the
cluster, and a refresh restarts the Ceph daemons.

Upgrade with the rolling-upgrade playbook, never with ad-hoc `snap refresh`:

```bash
cd /etc/ansible && ./run-playbook.sh storage/upgrade-microceph.yml
```

It gates on `ceph health`, upgrades one node at a time (`serial: 1`, dqlite
quorum needs 2/3), does the storage leader last, waits for the cluster to
settle between nodes, and re-applies the hold afterwards.

Check for pending refreshes:

```bash
# On each storage node:
snap list | grep held          # what is pinned, and at which revision
snap refresh --list            # what a refresh would install
```

## CVE tracking, self-built pins, and container images

Installed apt packages, self-built source pins (`frp`), and the public
container images are tracked against a local OSV vulnerability feed
(`osv-feed/`, see `docs/design/osv-feed.md` and
`docs/design/container-image-tracking.md`). The full version-controlled
perimeter inventory (components, tiers, node groups, pin sources, owners) is
`config/ansible/perimeter_software.yml` (see
`docs/design/perimeter-software-tracking.md`). The daily mirror + textfile
collector run on the collector node (`osv_feed_collector`, default
`core[0]`), and findings feed the `PerimeterCVE*LongPending` alerts through
the shared `perimeter_cve` Alertmanager route.

### Where the data lives

- Writable mirror DB: `/var/lib/osv-feed/db` (local disk, single writer).
- Published read-only snapshot: `/near1/osv-feed`
  (`osv-feed.sqlite`, `*.cursor`, `epss.json`, `acks.json`,
  `container/*.manifest`).
- Textfile output: `/var/lib/prometheus/node-exporter/osv_feed.prom` on the
  collector node.
- Acknowledgment store: `/near1/osv-feed/acks.json` (shared, so acks are
  node-independent).

The collector runs `osv-feed match --format prometheus` plus
`--container-dir /near1/osv-feed/container`, so host + container findings
are produced in one pass every 15 minutes. The mirror timer runs daily.

### Viewing current findings

On the collector node:

```bash
# Everything tracked (host categories + container manifests):
osv-feed --db-immutable match --format table

# Just the pending (unresolved) surface, with ack state:
osv-feed --db-immutable match --category perimeter --format table

# Container image findings, with the image label:
osv-feed --db-immutable match --container-dir /near1/osv-feed/container --format table
```

`--format json` includes each CVE's `summary`, `details`, `aliases`, and
`references`; `--verbose` prints the same detail under table rows.
`acked="true"` rows are visible for the audit trail but suppressed from the
alerts.

What actually appears in Prometheus:

- `osv_feed_cve_info{ecosystem,package,version,cve,severity,state,category,acked[,image]}`
- `osv_feed_cve_first_seen_seconds{...}` (persisted per finding; alerts use it)
- `osv_feed_cve_epss_score` / `osv_feed_cve_epss_percentile` (0..1)
- `osv_feed_mirror_present`, `osv_feed_mirror_age_seconds`,
  `osv_feed_collector_error` (feed health)

### PerimeterCVE alerts

- `PerimeterCVECriticalLongPending` — a critical pending CVE (`no_fix_yet`
  or `fix_available`) older than **12h**, non-acked.
- `PerimeterCVEHighLongPending` — a high pending CVE older than **2 days**,
  non-acked.

Both join `first_seen` against `osv_feed_cve_info` on
`ecosystem,package,version,cve,image`, so each container image stays a
distinct perimeter surface. They repeat daily (Alertmanager
`repeat_interval: 24h` for the `perimeter_cve` route) until resolved or
acked, and route by `severity` (critical vs warning receiver).

The window constants live in `config/ansible/monitoring/templates/ycluster-alerts.yml.j2`
(`12 * 3600` and `2 * 24 * 3600`).

### Acknowledging / deciding on a CVE

Acknowledging keeps the finding visible in `match` output but marks it
`acked="true"` so the alerts stop. Acks are keyed by
`category + ecosystem + cve` (a container ack therefore applies to that CVE
across all images, since ack keys do not carry `image`).

```bash
# On the collector node (config already points --ack-db at /near1/...):
osv-feed ack add --category perimeter --ecosystem Debian --cve DEBIAN-CVE-XXXX-00000 \
  --note 'mitigated at firewall'
osv-feed ack list --category perimeter --format json
osv-feed ack remove --category perimeter --ecosystem Debian --cve DEBIAN-CVE-XXXX-00000
```

Only acknowledge a CVE when it is actually mitigated (documented exception),
not to silence noise — `no_fix_yet` entries are the cases that need a
lockdown decision rather than a "wait for the upstream fix" posture.

### Bumping self-built pins (frp)

`frp` is tracked against the OSV `Go` ecosystem with the `name=version`
form, pinned to the exact version we build:

- Pin in `app/build-frp.yml` (`frp_repo_sha`, plus `version 0.70.1`).
- Tracking spec in `monitoring/install-osv-feed.yml`
  (`osv_feed_selfbuilt_packages: ["github.com/fatedier/frp=0.70.1"]`).

When bumping: rebuild from the new SHA with `app/build-frp.yml`, update the
`frp=…` spec to the new version, and redeploy `install-osv-feed.yml` so the
mirror/Go ecosystem and `[tracking.selfbuilt]` land on the collector.
`deps-vuln-src` on the `ext/` checkout remains the zero-lag cross-check.

### Container image pins and manifest regeneration

Each public image is tracked via a manifest at
`config/ansible/monitoring/container-manifests/<image>.manifest` (vendored, synced to
`/near1/osv-feed/container` by `install-osv-feed.yml`). The manifest is
generated deterministically from the pinned source tree via
`manifestgen`; dev-only tooling is excluded with `--uv-select`.

| Image | Pin source | Manifest generation |
|---|---|---|
| open-webui | `app/build-open-webui.yml` `webui_repo_sha` | `--uv-select base` |
| zulip | `app/build-zulip.yml` (docker-zulip + src SHAs) | `--uv-select prod` |
| authentik | `app/install-authentik.yml` `authentik_image` (digest) + `authentik_version` | `--uv-select base` |

To regenerate a manifest after an image/lockfile bump:

```bash
# From a checkout of the pinned tree containing uv.lock:
python3 -m osv_feed.manifestgen open-webui \
  --uv uv.lock --uv-select base \
  --comment "open-webui v0.11.0 <sha>" \
  --out open-webui.manifest
```

Commit the regenerated manifest and redeploy `install-osv-feed.yml` so it
syncs to `/near1/osv-feed/container`. Re-run the collector match to confirm
the new surface before relying on the alerts.

**authentik digest pinning** — authentik runs an upstream image, previously
a mutable `:tag`. It is pinned by digest:
`authentik_image: ghcr.io/goauthentik/server@sha256:<digest>` in
`app/install-authentik.yml`; `authentik-compose.yaml` lets
`AUTHENTIK_IMAGE` override the whole reference, and `authentik-manager`
writes it to the compose environment alongside `AUTHENTIK_TAG`. When bumping
authentik: pull/verify the new amd64 digest, update `authentik_version`
and `authentik_image`, regenerate the manifest from that tag's
`uv.lock`/pip surface, and deploy `app/install-authentik.yml`.

### Validating the alert rules

The alert rules are a Jinja template
(`monitoring/templates/ycluster-alerts.yml.j2`, configured with `[%`/`%]`
delimiters). After any change, render and parse-check it:

```bash
.venv/bin/python - <<'PY'
from jinja2 import Environment, FileSystemLoader
import yaml
env = Environment(loader=FileSystemLoader('config/ansible/monitoring/templates'),
                  variable_start_string='[%', variable_end_string='%]',
                  trim_blocks=True, lstrip_blocks=True)
out = env.get_template('ycluster-alerts.yml.j2').render()
open('tmp/ycluster-alerts.rendered.yml', 'w').write(out)
yaml.safe_load(out)
print('renders + parses OK')
PY
```

Then, with a local Prometheus binary available:

```bash
promtool check rules tmp/ycluster-alerts.rendered.yml
```

### When to escalate

- `osv_feed_collector_error == 1` — the match run failed; check the
  collector service logs and the feed snapshot.
- `osv_feed_mirror_age_seconds` above ~2 days — the daily mirror is stale,
  so findings (and the absence of findings) cannot be trusted. Re-run
  `osv-feed mirror` (all ecosystems) and re-publish.
- `PerimeterCVE*LongPending` firing on a `no_fix_yet` CVE on a P0-U/P0-A
  image or package — treat as the perimeter SLA breach; acknowledge only
  with a documented mitigation, and get a fix or exception on the calendar.
