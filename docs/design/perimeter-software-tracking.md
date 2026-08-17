# Perimeter Software Update Tracking

## Purpose

The cluster's general update posture is:

- apt security patches via `unattended-upgrades` plus held-package handling
  (`docs/operations/packages.md`),
- monthly-ish dependency hygiene for pinned Go/Python/source-build surfaces
  (`docs/design/dependency-hygiene.md`),
- the CVE-tracking gap recorded in `todo/security/cve-tracking.md`.

That general posture is not tight enough for components that sit on or near the
external boundary. A critical in `frps`, `sshd`, `nginx`, or a public-facing
container takes a different risk path than the same bug in an interior service.
This document defines the perimeter inventory and a closer tracking/update plan
for it.

## Perimeter model

P0 components are externally reachable and are split by whether an attacker can
reach the component's meaningful surface without credentials:

- **P0-U — unauthenticated.** Raw internet traffic reaches this component
  before any credential gate (TLS termination, HTTP/proxy entry, login pages,
  ACME, public status/health endpoints).
- **P0-A — authenticated.** Externally reachable, but a credential or key
  (frp token, SSH key/TOTP, IdP session, API key) is required before the
  component's non-trivial functionality is reachable. These still need close
  tracking, but exploitable authentication gaps are treated separately from
  unauth exposure.

P1 and P2 remain the remote/trust and interior tiers from the earlier model.

| Tier | Surface | Where it runs | Update/deploy mechanism today |
|---|---|---|---|
| P0-U | `frps` WAN listeners for public HTTP/HTTPS and dynamically bound public ports | frontend nodes | source build pin in `app/build-frp.yml`; installed by `app/install-frp-server.yml` |
| P0-U | `frpc` public HTTP/HTTPS tunnel, public nginx listeners/vhosts (ACME HTTP-01 challenge surface) | storage leader | same frp pin; `app/install-frp.yml`; apt `nginx` + certbot-manager rendered sites |
| P0-U | Public app pre-auth surface: login pages, static assets, health/unauthenticated routes, registration/redirect paths | storage leader containers | authentik's own login/registration surface (the IdP is its own gate, not consumed through one) |
| P0-A | frp control channel (`bindPort`) and tunnel registration/token path | frontends + core frpc clients | frp pin; token/config in etcd, rendered at service start |
| P0-A | Per-core SSH tunnels | core nodes | frp pin; `app/install-frp.yml` |
| P0-A | VM-access bastion `frpc` + jump sshd | Incus/Nvidia VM hosts | `admin/install-vm-bastion.yml`, in-container binary push |
| P0-A | Open WebUI (chat.<domain>): local signup + password login disabled, OIDC via authentik is the only path in | storage leader | source-built image + `app/build-open-webui.yml` pin; osv-feed manifest |
| P0-A | Zulip (zulip.<domain>): SAML-only steady state (`zulip_auth_backends: SAMLAuthBackend`), local password login disabled, authentik as IdP | storage leader | source-built image + `app/build-zulip.yml` pin; osv-feed manifest |
| P0-A | ComfyUI (comfy.<domain>): nginx forward-auth gate before the app | storage leader (vhost) + GPU host (backend) | `app/install-comfyui.yml` buildah image; not yet osv-feed scanned |
| P0-U | OpenSSH server (pre-auth wire protocol, KEX, auth negotiation) + admin TOTP/SSH key path | frontends, core, bastion containers, managed nodes | apt `openssh-server` + `unattended-upgrades`; config from setup playbooks |
| P0-A | IdP/authentik and authenticated web endpoints behind forward-auth | storage leader | authentik image version var; nginx forward-auth |
| P1 | certbot + python3-certbot-nginx (outbound ACME renewal client) | storage leader | apt `certbot`, `python3-certbot-nginx` |
| P0 guardrail | Frontend host firewall | frontends | apt `ufw`, managed by frp-server/monitoring playbooks |
| P0-U | WireGuard endpoint (public UDP handshake processed before key) + remote peers | gateway VIP holder + remote nodes | apt `wireguard`/`wireguard-tools`; keys/config in `ycluster wg` |
| P1 | Gateway NAT, keepalived, DHCP/DNS, PXE, squid, bootstrap HTTP | core nodes | apt `keepalived`, `dnsmasq`, `squid`, nginx/PXE artifacts |
| P1 | SSH jump/VM bastion allow-list | VM hosts | `ycluster vm bastion-sync`; sshd keys in etcd |
| P1 | Inference edge | storage leader | `local-ai-proxy` Go module (S1 dependency hygiene) |
| P2 | Runtimes: docker, Incus/LXC, QEMU, podman, runc | storage/compute/Nvidia hosts | apt/snaps/pinned source builds |
| P2 | etcd mTLS, registry, cluster CA | core/storage | etcd deployment + cert/key lifecycle |
| P2 | prometheus-blackbox-exporter (outbound prober) | perimeter probes | apt `prometheus-blackbox-exporter` |
| P2 | Host kernels, openssl/libssl, systemd, DPKG base | all managed nodes | apt + unattended-upgrades |

P0-A *is* a standalone inventory tier: a component that is externally
reachable but whose meaningful surface sits behind a credential/key gate is
tiered P0-A, not subsumed under a P0-U neighbor. The gate can be nginx
forward-auth (ComfyUI, admin-api) or a disabled-local-login IdP-only app where
authentik is the only path in (Open WebUI: `ENABLE_SIGNUP=False` /
`ENABLE_LOGIN_FORM=False`, OIDC via authentik). Being adjacent to the IdP does
not make a component P0-U on its own. The pre-auth rule is about whether the
component itself processes untrusted data on its reachable surface:
- **P0-U** because they process untrusted pre-auth data themselves: `sshd`
  (wire protocol/KEX before key), WireGuard (handshake before key), `frps`
  (WAN listener), and the `nginx` TLS/HTTP entry itself.
- **P0-A** because a gate keeps untrusted pre-auth bytes off them: Open WebUI
  and Zulip (authentik OIDC / SAML-only is the only login path — local
  password login disabled), ComfyUI (nginx forward-auth), frp control/tunnel
  registration path, per-core SSH tunnels, VM bastion.

The credential-gated *sub-surfaces* of a P0-U component (e.g. frp's control
channel on top of the public tunnel, or authenticated web behind forward-auth)
still belong here, but they do not define the component's tier — a component
with any P0-U surface is tiered P0-U by its worst exposure.

Frontend nodes are deliberately separate from the `managed` fleet: they run a
smaller package set and should be updated as their own play explicitly, not
through bulk managed-node flows.

## Current perimeter gaps

- **No runtime perimeter inventory.** `unattended-upgrades` is active on
  frontends and core, but there is no metric/alert for installed versions of
  `openssh-server`, `nginx`, `openssl`, `frps`/`frpc`, `keepalived`,
  `wireguard-tools`, `dnsmasq`, `squid`, or the frontend firewall.
- **P0-U and P0-A are not separated in policy.** Today an unauth web/proxy
  CVE and an SSH/frp-control CVE are treated the same. The tracking plan should
  carry the tier label all the way through collector metrics, alerts, and SLA
  rows so unauth exposure gets the shorter window.
- **frp is only tracked as a source pin.** `deps-vuln-src` checks an `ext/`
  checkout, not the actual `/opt/src/frp` buildhost tree or the installed
  binaries. `app/build-frp.yml` also skips rebuilding if the old binaries
  already exist, so bumping `frp_repo_sha` alone can leave old binaries in
  place. A perimeter upgrade needs a verifiable rebuild-on-pin-change path.
- **Rathole is not retired on frontends.** The repo rip-out excludes frontend
  nodes, and the legacy rathole service is still active there for external
  admin clients. Rathole upstream is dormant, so it cannot be treated as a
  maintainable P0 component; it should be removed from the perimeter, not
  tracked.
- **Container images are not scanned or digest-pinned.** Images such as
  authentik, open-webui, zulip, and `registry:2` are not covered by
  `dependency-hygiene.md` (deferred there) and have no runtime CVE/digest
  alert.
- **No SLA or owner for perimeter updates.** `HeldPackagesStale` covers held
  packages, not high/CVEs in perimeter components. HTTPS/blackbox and cert
  alerts cover availability and certificates, not vulnerability status.

## Tracking plan

### Phase 1 — tracked perimeter inventory

> **Status: landed (repo).** The version-controlled inventory now lives at
> `config/ansible/perimeter_software.yml` (17 components with tier, `osv_category`,
> and osv package list), kept
> current with the playbooks in this repo. `monitoring/install-osv-feed.yml`
> loads this file and derives its `[tracking.*]` package lists from it, so the
> inventory is the operational source of truth for CVE matching. Phase 2's runtime version metric
> (`ycluster_perimeter_package_info`) is not yet emitted; the CVE tracking and
> alert wiring are live (see `docs/design/osv-feed.md` and
> `docs/operations/packages.md`).

Land a version-controlled perimeter inventory, ideally as one ansible vars
file (`config/ansible/perimeter_software.yml`) plus this doc as the readable
view:

- Consumed fields only: component `name`, `tier` (`P0-U`, `P0-A`, `P1`, `P2`),
  `osv_category`, and `osv_packages`. Package/bin name, pin source, manifest path,
  node groups, cadence, restart rule, verification step, and owner are human
  narrative — written as comments in the vars file or in the phase tables
  below / linked playbooks, so no YAML key is ever mistaken for a machine
  input. Keeping the inventory to its load-bearing fields avoids drift.
- Keep the pin source in the existing playbooks; the inventory only references
  it so a bump is discoverable.

The file should list at minimum: `frps`, `frpc`, `openssh-server`,
`openssl`, `nginx`, `certbot`, `python3-certbot-nginx`, `ufw`,
`iptables`, `keepalived`, `wireguard`, `wireguard-tools`, `dnsmasq`,
`squid`, `prometheus-blackbox-exporter`, docker/Incus/podman/runc,
and the public container image pins/digests.

### Phase 2 — runtime version collection

Add a small textfile collector (same pattern as existing node-temp/backup
collectors) on P0-U/P0-A/P1 nodes:

- gather `dpkg-query -W` for the perimeter package list,
- `frps --version` / `frpc --version`,
- `nginx -v`, `openssl version`, `snap list` where relevant,
- `/opt/src/frp` git HEAD on the buildhost,
- container image digests/versions on the storage leader,
- systemd state for `frps`, `frpc`, `frpc-ssh`, `nginx`, `ssh`,
  `keepalived`, `wg-quick@wg0`, `dnsmasq`, `squid`.

Emit to the existing node-exporter textfile dir, e.g.
`ycluster_perimeter_package_info{pkg="frp",tier="p0-u",version="0.70.0"} 1`
and a comparison metric against the expected ansible pin. Existing
`CollectorStale`-style alerting gives freshness, and a new
`PerimeterVersionStale` alert catches drift.

### Phase 3 — advisory and freshness feeds

Extend the existing dependency gate rather than creating a parallel scanner:

- apt perimeter packages: weekly `apt-get -s upgrade`/`apt list --upgradable`
  report plus an Ubuntu/USN security-source check for the short package list;
  alert when a security update for a P0-U or P0-A package is available but not
  yet applied within SLA. The local OSV feed (`osv-feed/`, see
  `docs/design/osv-feed.md`) implements the leak-free Ubuntu/Debian/Go
  mirror and three-state matcher
  (`patched` / `fix_available` / `no_fix_yet`) without posting installed
  versions anywhere; wire its `--format prometheus` output
  into the textfile collector and use the first-seen gauge for the pending
  CVE SLA.
- frp/source pins: the osv-feed `Go` ecosystem already tracks `frp`
  against the build pin (the `[tracking.selfbuilt]` category pins
  `github.com/fatedier/frp=<pin version>`); keep `deps-vuln-src`, and also
  check the actual buildhost
  `/opt/src/<tree>` HEAD against the playbook pin and verify the installed
  binary version matches the pin.
- containers: add a source-built image scanner (for example `trivy` via
  `go install`, consistent with the no-prebuilt-binaries rule) run on the
  registry images and public apps, plus digest pinning in the install
  playbooks so "what was scanned" is "what runs".

### Phase 4 — perimeter update path

Extend the existing install playbooks with a deliberate, staged upgrade path
(`admin/upgrade-perimeter.yml` or an explicit sequence around `build-frp.yml`):

1. Build/refresh source pins on the buildhost, verify the SHA and binary
   version before installing anything.
2. Update frontend `frps` one frontend at a time; `ufw status`, frp control
   health, and external blackbox probes must pass before the next frontend.
3. Update storage-leader `frpc`/`nginx` and certbot; `nginx -t` before reload,
   then verify external HTTPS through the public path.
4. Update core `frpc-ssh`; verify SSH jump from the bastion/admin path.
5. Update VM-bastion `frpc` on Incus hosts; smoke-test `ssh -J` to a VM.
6. Update WireGuard nodes separately if the kernel/tooling changes; verify
   peer connectivity after each side.

For apt:
- keep `unattended-upgrades` as the general patch channel,
- use `apt state=latest` only for the explicit perimeter package list on
  perimeter hosts, never a broad dist-upgrade in the same run,
- reload where possible (`nginx -t` + reload), restart serial where required
  (`keepalived`, leader-gated services), and never restart PostgreSQL or
  microceph outside their existing rolling playbooks.

### Phase 5 — alerts and SLAs

Proposed alerts (monitoring/templates/ycluster-alerts.yml.j2):

- `PerimeterVersionStale` — installed version does not match the expected
  playbook pin/binary on a P0-U/P0-A/P1 node.
- `PerimeterAptUpdatePending` — a security update for a listed P0 package has
  been available for longer than the SLA; carry `tier="p0-u"` or
  `tier="p0-a"` so unauth exposure pages first.
- `PerimeterImageOutdated` — running image digest/tag does not match the
  pinned digest.
- `PerimeterCVECritical` / `PerimeterCVEHigh` — scanner output touches a P0-U,
  P0-A, or P1 component.
- `PerimeterCVECriticalLongPending` / `PerimeterCVEHighLongPending` — same
  scanner signal has been pending past the SLA window
  (`osv_feed_cve_first_seen_seconds` vs wall clock); the perimeter route in
  Alertmanager uses a 24h `repeat_interval` so unresolved CVEs stay visible
  daily. `no_fix_yet` findings page separately so a lockdown decision is
  triggered rather than hidden inside a fix-available list.
- `RatholeLegacyActive` — until the frontend rathole cleanup lands, alert if
  the service is running so the removal stays visible.

SLA proposal:

| Severity/tier | Detect | Decide | Deploy |
|---|---|---|---|
| P0-U critical CVE / auth-bypass | within 12h | within 24h | within 72h unless a documented mitigation/exception |
| P0-U high CVE | within 1 week | within 1 week | within 14 days |
| P0-A critical CVE | within 1 day | within 2 days | within 7 days unless a documented mitigation/exception |
| P0-A high CVE | within 1 week | within 1 week | next perimeter window (target ≤30 days) |
| P1/P2 high | weekly report | monthly `deps-check` review | monthly or next maintenance window |
| Source pin drift | weekly report | monthly | reviewed re-pin, never auto-write |

### Phase 6 — cadence, ownership, and runbook

- Weekly: automated perimeter collect + advisory report (`tmp/`, gitignored);
  route the generated summary to the existing alerting channel.
- Monthly: include the perimeter report in the existing `deps-check` review;
  reassess accepted findings.
- Per-pin change: update `build-*.yml`, rebuild, verify versions, deploy via
  Phase 4, and leave the previous known-good pin/version in the run notes for
  rollback.
- Owner: same component-owner convention as `docs/design/dependency-hygiene.md`
  (top committer per component), with a single security owner accountable for
  P0-U/P0-A SLA breach.
- Extend `docs/operations/packages.md` and `docs/operations/public-routing.md`
  with the runbook; close `todo/security/cve-tracking.md` when the weekly
  advisory report is live.

## First actions

1. Remove rathole from frontends and migrate/revoke the legacy admin clients;
   delete `app/uninstall-rathole.yml`'s frontend exclusion and scrub the binary
   once the external clients are gone.
2. Fix `app/build-frp.yml` so a pin change forces a verified rebuild and the
   installed binary records its source SHA.
3. Land the perimeter inventory vars + textfile collector with `p0-u`/`p0-a`
   tier labels.
4. Wire the `osv-feed` mirror (DB on `/near1`, configurable) and
   `match --format prometheus` into the collector; see
   `docs/design/osv-feed.md`.
5. Add the Phase 5 alerts with the separate P0-U/P0-A SLA rows.
