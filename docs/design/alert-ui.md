# Alert UI for Prometheus/Alertmanager

## Status

Deployed. The `v0.134` image is built and pushed, the container runs on every
core node aggregating all core Alertmanagers, `/karma/` is served behind
Authentik forward auth on the public vhost (admin-only) and without auth on the
cluster-internal vhost, the admin nav links it as "Alerts", and Prometheus
scrapes Karma's `/metrics`.

## Problem

Prometheus alert rules are evaluated per node, shipped to the HA Alertmanager
instances on the core nodes, and delivered through `ntfy-webhook` to ntfy. The
only surface for looking at what is firing is each Alertmanager's own web UI on
`:9093`:

- it is per instance, so the operator has to check `s1`, `s2`, `s3` separately
  and mentally deduplicate the HA copies;
- it has no authentication and listens on all interfaces on the cluster
  network (`docs/design/intrusion-detection.md`, "Alertmanager listens on all
  interfaces on 9093 with no auth");
- grouping follows Alertmanager's `group_by` and cannot be re-cut by the user;
- there is no filter language beyond what the URL carries, no cross-alert
  overview, and no history.

Silences are the one write operation the UI supports today, and they are
created anonymously.

What we need from a UI: browse the merged firing set across the HA peers,
filter/group by label, see silences and inhibitions, optionally create and
expire silences with an identified author, and sit behind Authentik like the
rest of the admin area. Delivery must stay on the single
Alertmanager -> ntfy path; a UI is an inspection surface, not a second router.

## Constraints

- **No prebuilt third-party binaries** (`docs/CONTRIBUTING.md`): a new component
  has to be apt, a source build in `build-<name>.yml`, or an image built
  in-cluster and pushed to `registry.xc:5000`. Pulling an upstream container
  image is not the compliant path.
- **Single delivery path.** The UI must read from Alertmanager's API. It must
  not ingest alerts itself and must not become a second notification path.
- **No new state to back up.** The UI should be stateless; Alertmanager stays
  the only alert store.
- **No wider API exposure.** Alertmanager must stay reachable by Prometheus and
  `ids-forwarder` over the cluster network. A UI that makes browsers talk to
  `:9093` directly would widen that exposure rather than contain it.

## Current deployment

Verified live on the current leader:

- Alertmanager `0.26.0+ds`, `prometheus-alertmanager.service`, listening on
  `*:9093`, UI/API unauthenticated.
- Grafana `12.1.1` in a `grafana/grafana` container on every core node,
  published on `:3030` by Docker and proxied at `/grafana/` behind Authentik
  forward auth.
- Grafana provisioning currently contains only `datasources` and `dashboards`;
  no alerting resources and no Alertmanager data source.
- Grafana runs with `GF_AUTH_ANONYMOUS_ENABLED=true` and the `Viewer` role.

## Options

| Option | Multi-AM / HA merge | Silence write | Identity / SSO | Deployment cost | Verdict |
|---|---|---|---|---|---|
| Alertmanager built-in UI | no | yes | none | already deployed | status quo, break-glass only |
| Grafana Alertmanager data source | one URL per data source | yes, subject to Grafana role | Authentik via Grafana `auth.proxy`, not configured today | very low: provisioning + nav link | best low-effort read view |
| Karma (prymitive/karma) | yes, cluster-aware | yes, with ACLs and one-click ack | header auth from Authentik | medium: new build + service + proxy | recommended for triage |
| Alerta | no (webhook ingest) | its own model | own users/API keys | high: DB + second alert store | reject |
| Nightingale (n9e) | replaces the alert engine | its own model | own users | high: second monitoring platform | reject |
| Grafana OnCall | n/a | n/a | n/a | archived (`grafana-cold-storage/oncall`, last push 2026-03) | reject |
| Keep | no (AIOps workflows) | its own model | own users | high: Postgres, Next.js UI | reject |
| Perses | alerting via plugins | plugin-defined | Grafana-compatible auth | medium, immature | reject for now |
| raystack/siren | alert framework | n/a | own users | last push 2024-07 | reject |
| shamubernetes/siren | single AM dashboard | limited | none | very new, single author, GPL-3.0 | reject |

## Grafana Alertmanager data source

Grafana ships an Alertmanager data source plugin; it is present in the running
`12.1.1` container. Adding it is a provisioning change, not a new service:

```yaml
- name: Alertmanager
  type: alertmanager
  uid: alertmanager-local
  access: proxy
  url: http://host.docker.internal:9093
  jsonData:
    implementation: prometheus
```

With the `prometheus` implementation, silences are read/write and contact
points, notification policies and templates are read-only (the Prometheus
Alertmanager API has no write path for them). Alertmanager resources appear
under Alerting -> Alert activity -> Active notifications (with an Alertmanager
drop-down, label/state filters and a custom group-by) and Alerting -> Silences.

Limits:

- one URL per data source, so each Grafana instance sees its local Alertmanager
  only; HA peers share state, but there is no automatic peer failover in the
  data source;
- Grafana is anonymous `Viewer` today, so the view is read-only in practice.
  Creating silences needs an authenticated Grafana user with silence
  permissions, which means turning on Grafana `auth.proxy` and mapping the
  Authentik identity nginx already resolves to Grafana's header-auth user
  header, instead of anonymous access;
- the alert UI stays coupled to the dashboard stack, including its
  `grafana/grafana:latest` drift.

This is the cheapest way to get a merged, filtered view. It does not give us
silence authorship from Authentik without a Grafana auth change.

## Karma

`prymitive/karma` is the established Alertmanager dashboard: Go backend, React
frontend, one static binary, Apache-2.0.

- Latest release `v0.134` (2026-10-22); repository active (last push
  2026-11-03).
- Requires Alertmanager >= 0.22; its test matrix covers 0.22 through 0.27 and
  0.31, which includes our 0.26.
- Aggregates alerts from several Alertmanagers, deduplicates identical label
  sets, tags alerts with `@alertmanager`/`@cluster`, and preserves each
  receiver's `group_by`.
- User-side grouping and filtering, multi-grid by label, inhibited-by
  inspection, silence deduplication, silence management with per-operation
  ACLs, and one-click acknowledgement that creates a short-lived silence.
- Optional alert history: queries the Prometheus that sent each alert (the
  `source` field) for the last 24h. This needs those Prometheus URLs to be
  reachable from Karma; otherwise disable `history`.
- Authentication by reverse-proxy header: the configured header must be
  present or the request is 403, and the captured value becomes the silence
  author. This maps directly onto nginx's `X-Authentik-UID`.
- `/health` and `/metrics` are exempt from header auth, so health checks and
  Prometheus scraping work without a login.
- `listen.prefix` supports `/karma/`, so it can sit behind the existing
  admin-api nginx pattern.

Important configuration choice: run every Alertmanager upstream with
`proxy: true`. Then grace-period silence writes go browser -> Karma ->
Alertmanager. With the default `proxy: false` the browser would be sent to
`:9093` directly, which re-exposes the unauthenticated Alertmanager API to
users' browsers and bypasses the gate.

Costs: it is a new component to build and upgrade, its UI is a JavaScript SPA,
and alert history needs `--web.external-url` to be usable on each Prometheus.
None of these conflict with the constraints above.

## Rejected options

- **Alerta** (Python, active): an alert store reached by webhooks. It would
  duplicate Alertmanager's dedupe/grouping, and its silences are not
  Alertmanager silences. That contradicts "Alertmanager remains the only
  routing and deduplication layer".
- **Nightingale** (Go, active): a full metrics/logs alerting platform with its
  own TSDB and rule engine. Adopting it is a monitoring-platform migration, not
  a UI.
- **Grafana OnCall**: the OSS repository is archived under
  `grafana-cold-storage`; no further fixes.
- **Keep**: AIOps/workflow platform with Postgres and a Next.js frontend; the
  license is not a recognised SPDX license, and it would become a second alert
  store.
- **Perses**: CNCF dashboard project; Alertmanager support is a community
  plugin, not a mature alert console.
- **Siren** (both the Raystack framework and the recent single-author
  dashboard): stale or too immature to take a dependency on.

## Recommendation

Conditional go on Karma, with Grafana's Alertmanager data source as the
zero-new-component stopgap.

1. If the requirement is only "see what is firing in one place", add the
   Alertmanager data source to Grafana and link the Alert activity view from
   the admin nav. Accept that silences need a separate auth change.
2. If the requirement is alert triage - merged HA view, filtering, silences
   with an identified author, acknowledgement - deploy Karma. That is the
   option that actually replaces Alertmanager's UI rather than wrapping it.
3. Keep the Alertmanager UI on `:9093` as break-glass. Neither option removes
   the need to restrict that port to the cluster network.

## Implementation

- `config/ansible/app/files/karma.Dockerfile`: upstream's multi-stage build,
  with a `scratch` final stage instead of `gcr.io/distroless/static` (the
  build host cannot reach that registry; Karma is static and only talks plain
  HTTP to the cluster Alertmanagers).
- `config/ansible/app/build-karma.yml`: checks out `prymitive/karma` at the
  `v0.134` commit, verifies `git rev-parse HEAD`, builds with `buildx` and
  pushes `registry.xc:5000/karma:v0.134`. The build host reaches GitHub
  directly (verified), so the first run bootstraps the checkout; later runs
  never pull and rely on the pinned SHA.
- `config/ansible/monitoring/templates/karma.yaml.j2`: one upstream per
  Alertmanager in the `core` group, shared `cluster` name, `proxy: true`, header auth on
  `X-Authentik-UID`, `listen.prefix: /karma/`, history off.
- `config/ansible/monitoring/install-karma.yml`: `docker_container` on
  `core`, config mounted read-only, published as `127.0.0.1:{{ karma_port }}`,
  `cap_drop: ALL`, no-new-privileges, health probe.
- `config/ansible/monitoring/files/karma-custom.js`: Karma's `custom.js` hook.
  It reads the groups Karma parsed from `X-Authentik-Groups` and adds a
  "YCluster" link back to `/` only when `ycluster-admins` is present.
- `config/ansible/admin/files/admin-api-karma-proxy.conf` plus `location
  /karma/` blocks in `admin-api-locations.conf` (internal, identity `internal`)
  and `admin-api-authed-locations.conf` (public, Authentik UID as the silence
  author). The snippet is installed by `admin/setup-web-services.yml`.
- `admin/static/nav.js` and `admin/files/index.html` link `/karma/` as
  "Alerts"; `group_vars/all/main.yml` owns `karma_port`; `prometheus-local`
  scrapes Karma's `/metrics`; `monitoring/monitoring.yml` imports the play.

Deployment and verification:

```
cd /etc/ansible
./run-playbook.sh app/build-karma.yml
./run-playbook.sh monitoring/install-karma.yml --limit core
./run-playbook.sh admin/setup-web-services.yml --limit storage
./run-playbook.sh monitoring/install-prometheus.yml --limit core
```

Karma has no OIDC flow of its own: SSO is the Authentik forward-auth outpost,
which nginx already fronts for `/grafana/` and `/admin/`, with the resolved
UID passed as Karma's header identity and `X-Authentik-Groups` used for the
admin-only home link.

### What the first merged view showed

The Alertmanager peers do not hold the same alert set. On the first live poll
Karma merged 39 unique alerts from four upstreams, while one peer alone held
48 and the other three held three each. `ids-forwarder` explains the
difference: `delivery.go` tries each `-alertmanager-url` in order and stops
after the first success (the comment says "a single reachable instance is
enough"), while `docs/design/intrusion-detection.md` says it posts to every
core instance. As a result the IDS detections from a given host are visible
only on whichever Alertmanager is first in that host's list. Karma makes the
asymmetry visible; it does not fix the delivery gap.

## Verification

- `curl -fsS http://127.0.0.1:8080/health` on every core node.
- Karma's alert count matches the union of the three Alertmanager `/api/v2/alerts`
  responses, with duplicates collapsed.
- A silence created in Karma is visible on every Alertmanager peer.
- An unauthenticated browser request to `/karma/` is redirected to Authentik
  login; `/karma/health` and `/karma/metrics` stay reachable from localhost.
- Alert delivery still arrives in ntfy exactly once per semantic event (the UI
  must not add a receiver).

Verified at rollout: the public vhost returns `302` to the Authentik outpost
for browser navigations and `401` for fetch-style requests; the internal vhost
returns `200` for `/karma/`, `/karma/health` and `/karma/custom.js` and reports
identity `internal` with `ycluster-admins`; `up{job="karma"}` is `1` on every
core node.

## Open questions

- Who is allowed to create silences: every authenticated admin, or an ACL that
  restricts it to the on-call group?
- Do we want Grafana to authenticate users at all (for silence writes and
  per-user dashboards), independent of the alert UI decision?
- Should alert history be enabled, and if so, are the Prometheus `source` URLs
  reachable from the core nodes where Karma would run?
