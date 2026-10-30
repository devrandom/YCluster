# Package Management

## Apt freshness monitoring

`monitoring/install-apt-metrics.yml` installs a 15-minute textfile collector that
publishes apt cache/upgrade age and apt/dpkg lock state, plus a 12h
`apt-cache-update.timer` that runs `apt-get update` (stamp refreshed only on
success). It also installs a global `DPkg::Lock::Timeout` of 300s so a hung apt
run fails after 5 minutes instead of blocking unattended-upgrades for days.

Alerts: cache not successfully updated in over 24h, an apt job active for over
1h, an apt/dpkg lock held for over 30m, or the collector textfile stale for
over 30m.

Check current metrics on a node with:

```bash
cat /var/lib/prometheus/node-exporter/apt_status.prom
```

The collector also asks `needrestart -b` which services are still running
against replaced libraries and whether an installed kernel supersedes the
running one, publishing `ycluster_restart_service_pending{service=...}` and
`ycluster_restart_kernel_pending`. Storage keeps needrestart in list-only mode
(unattended ceph/etcd/keepalived/postgresql restarts cost availability), so
these two alerts are how a pending restart surfaces there:

- `StorageServiceRestartPending` — a storage service needs a restart; run
  `needrestart -r l` on the node and restart it in a maintenance window.
- `StorageKernelRebootPending` — a storage node still runs a superseded
  kernel; schedule a reboot.

Both aggregate over the storage nodes, so a fleet-wide condition pages once
instead of once per node.

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
- `osv_feed_cve_first_seen_seconds{...}` (persisted per finding)
- `osv_feed_cve_summary{severity,acked,cves}` — one series per alert severity
  (critical/high), value = count of non-acked pending findings past that
  severity's SLA, `cves` = up to 5 example CVE IDs; emitted only when
  non-zero.
- `osv_feed_cve_epss_score` / `osv_feed_cve_epss_percentile` (0..1)
- `osv_feed_mirror_present`, `osv_feed_mirror_age_seconds`,
  `osv_feed_collector_error` (feed health)

### PerimeterCVE alerts

- `PerimeterCVECriticalLongPending` — non-acked critical pending CVEs
  (`no_fix_yet` or `fix_available`) past **12h**, coalesced into one alert
  with a count + up to 5 example CVEs.
- `PerimeterCVEHighLongPending` — non-acked high pending CVEs past **2 days**,
  coalesced the same way.

Each fires from `osv_feed_cve_summary{severity=...,acked="false"} > 0`, where
osv-feed has already collapsed all pending, non-acked findings past that
severity's SLA into one series per severity (the `acked="false"` label makes
the ack state explicit; `cves` lists up to 5 examples). They repeat daily
(Alertmanager `repeat_interval: 24h` for the `perimeter_cve` route) until
resolved or acked, and route by `severity` (critical vs warning receiver).

The SLA window constants live in `osv-feed/osv_feed/cli.py`
(`CRITICAL_SLA_SECONDS` / `HIGH_SLA_SECONDS`); the per-CVE detail is still
available from the `osv_feed_cve_info`/`osv_feed_cve_first_seen_seconds`
series and `osv-feed match --format table`.

### Acknowledging / deciding on a CVE

Acknowledging keeps the finding visible in `match` output but marks it
`acked="true"` so the alerts stop. Acks are keyed by
`category + ecosystem + cve + [surface]`. A **surface** is an arbitrary
deployment target: a container image, or a non-container install such as the
self-built `local-ai-proxy` (Go ecosystem). An empty surface is a wildcard
(keeps the legacy three-part key), so a surface-less ack suppresses the CVE on
every surface, while a surface-scoped ack suppresses it only there. Matching
prefers the most specific ack (exact ecosystem + exact surface, then
wildcard surface, then wildcard ecosystem, then wildcard both), and it matches
the finding's `aliases` too, so acknowledging a GHSA also suppresses the same
advisory when another feed reports it as `PYSEC-…` or `CVE-…`.

```bash
# On the collector node (config already points --ack-db at /near1/...):
osv-feed ack add --category perimeter --ecosystem Debian --cve DEBIAN-CVE-XXXX-00000 \
  --note 'mitigated at firewall'
# Scope an ack to one image so the same CVE stays alerting on other images:
osv-feed ack add --category container --ecosystem PyPI --cve GHSA-XXXX-XXXX-XXXX \
  --surface open-webui --note 'unreachable on open-webui wiring'
osv-feed ack list --category perimeter --format json
osv-feed ack list --format json          # see the surface column / filter with --surface
osv-feed ack remove --category container --ecosystem PyPI --cve GHSA-XXXX-XXXX-XXXX \
  --surface open-webui
```

Only acknowledge a CVE when it is actually mitigated (documented exception),
not to silence noise — `no_fix_yet` entries are the cases that need a
lockdown decision rather than a "wait for the upstream fix" posture. Prefer a
`--surface`-scoped ack when the review only covered one deployment; a blanket
ack suppresses the CVE everywhere it appears, including surfaces that were not
reviewed.

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
`/near1/osv-feed/container` by `install-osv-feed.yml`). Lockfile-backed
manifests use `manifestgen`; the open-webui manifest reflects the built
image's actual `pip list`, because the Dockerfile installs `requirements.txt`.

| Image | Pin source | Manifest generation |
|---|---|---|
| open-webui | built image from `app/build-open-webui.yml` `webui_repo_sha` | `pip list --format=json` from the built image; authlib/certifi/pillow/Brotli are patched direct pins |
| zulip | `app/build-zulip.yml` (docker-zulip + src SHAs) | `--uv-select prod` |
| authentik | `app/install-authentik.yml` `authentik_image` (digest) + `authentik_version` | `--uv-select base` |
| forgejo | patched in-cluster image from `app/build-forgejo.yml` (upstream digest + brotli/expat/zlib security bumps) | `manifestgen forgejo --apk-installed <installed> --apk-release v3.24 --go-buildinfo <buildinfo>`; apk specs use the installed DB's `o:` source origin, Go specs use the linked binary's buildinfo |
| vikunja | `app/install-vikunja.yml` `vikunja_upstream_image` (amd64 digest) + `vikunja_version` | `manifestgen vikunja --go-buildinfo <buildinfo>` from the distroless binary; no apk layer |

Alpine advisories are keyed on the **source** package, while
`/lib/apk/db/installed` records binary subpackages (`P`, e.g.
`brotli-libs`) with their origin in `o:`. Emit `o:` (falling back to `P`
only when no origin is recorded), or the matcher silently misses advisories
such as `ALPINE-CVE-2026-111497` for `brotli`.

To regenerate a manifest after an image/lockfile bump:

```bash
# From the built open-webui container:
docker exec <container> python -m pip list --format=json \
  > ./tmp/openwebui-piplist.json
# Convert each {"name","version"} pair to PyPI:<name>=<version> and
# write the version-pinned manifest (see the existing manifest header).

# From the built Forgejo image:
docker create --name forgejo-extract <image>
docker cp forgejo-extract:/lib/apk/db/installed ./tmp/forgejo-installed
docker cp forgejo-extract:/app/gitea/gitea ./tmp/forgejo-gitea
docker rm forgejo-extract
go version -m ./tmp/forgejo-gitea > ./tmp/forgejo-buildinfo
PYTHONPATH=osv-feed python3 -m osv_feed.manifestgen forgejo \
  --apk-installed ./tmp/forgejo-installed --apk-release v3.24 \
  --go-buildinfo ./tmp/forgejo-buildinfo \
  --out config/ansible/monitoring/container-manifests/forgejo.manifest

# From the pinned Vikunja image (distroless: only the Go binary):
docker create --name vikunja-extract <image>
docker cp vikunja-extract:/app/vikunja/vikunja ./tmp/vikunja
docker rm vikunja-extract
go version -m ./tmp/vikunja > ./tmp/vikunja-buildinfo
PYTHONPATH=osv-feed python3 -m osv_feed.manifestgen vikunja \
  --go-buildinfo ./tmp/vikunja-buildinfo \
  --out config/ansible/monitoring/container-manifests/vikunja.manifest
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
