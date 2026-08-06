# Zulip on the cluster

## Status

**Draft — direction decided, nothing implemented. (Refreshed 2026-08-06
against current repo + upstream Zulip source; see "Confirmed decisions".)**

Decided so far:

- Deploy via the official `docker-zulip` compose stack, on the storage
  leader, following the same shape as Open-WebUI and Authentik
  (containerized, leader-only, state on `/rbd/misc` and `/rbd/user`).
- Move the database into the cluster's shared Postgres
  (`/rbd/user/postgres`, already serving Authentik + Open-WebUI), so
  backups and upgrades follow the same path as every other cluster
  app — drop the bundled `zulip-postgresql:14` container.
- Keep bundled memcached / rabbitmq / redis (ephemeral-ish, no
  cluster-wide backup story; bundling is the simpler choice).
- Auth is SAML against the existing cluster IdP (Authentik).
- **No outbound SMTP.** Use Django's `console` email backend;
  invitations and notifications get delivered out-of-band or via
  in-app UI respectively.
- Build the Zulip image in-cluster from source via a
  `build-zulip.yml` playbook; no prebuilt binary downloads (matches
  the cluster's no-third-party-binaries rule — see `AGENTS.md`).
- Expose internally at `zulip.xc` (storage VIP) and externally at
  `zulip.<domain>`, behind the frp/nginx/certbot pipeline now used by
  `app.<domain>`, `admin.<domain>`, and `comfy.<domain>` (see
  `docs/design/frp-migration.md`) — not the retired rathole frontend.

### Confirmed decisions (2026-08-06)

Sorted out with the operator before implementation:

- **Version pin:** docker-zulip release tag `12.1-0` (2026-06-26). The
  vendored `ext/docker-zulip` clone is a newer *master* SHA, not a
  release; the build playbook must pin to the tag (`ZULIP_GIT_REF=12.1`
  in the Dockerfile). Local `ext/` dirs are gitignored clones, not
  submodules.
- **Postgres auth:** the `zulip` role connects with `scram-sha-256`
  password auth. No new `pg_hba.conf` rule is needed — the install
  playbook (`config/ansible/storage/install-postgres.yml`) already adds
  `host all all 172.0.0.0/8 scram-sha-256`, and Authentik/Open-WebUI
  connect the same way via `host.docker.internal`. Only create the
  `zulip` DB + role.
- **Collation:** `env.sh`/`configure-locale.yml` set `LC_ALL=C.UTF-8`,
  so `C.UTF-8` is expected; still verify `SHOW LC_COLLATE` on the
  leader during step 2 (see the wrinkle below).
- **Soak:** not a per-user migration requirement. `EmailAuthBackend` is
  bootstrap-only (create the realm + first owner; Zulip documents that
  it can be disabled after). SAML matches every account by email at
  login, so existing/invited users don't each need a prior SAML login.
  The "soak" is reduced to a short optional safety window. See
  "Auth integration" and "Account migration".
- **Network isolation:** Zulip does *not* adopt MinerU's full
  `--network=none` + UDS pattern — the app container needs the
  shared Postgres plus memcached/rabbitmq/redis, so it can't run
  with no netns. We instead adopt the ingest *and* egress sides of
  that pattern separately: no host-wide published ports (loopback-only
  web port as the sole publish), port 25 and 443 unpublished,
  host-side nginx as the single controlled ingress, **and** a
  `DOCKER-USER` host egress filter that pins the compose subnet and
  allows Postgres-only outbound (applied unconditionally on all
  storage nodes — source-scoped to the pinned subnet, so it's inert
  off the leader; a firewall change staged in the playbook and
  signed off per `AGENTS.md`).
  See "Network isolation (applying the MinerU sidecar pattern)".

Next: implementation starts with `build-zulip.yml` +
`install-zulip.yml` after this design is reviewed.

## Motivation

The cluster has chat-shaped communication happening in ad-hoc places
(operator announcements on individual Zulip-topic threads in
GitHub issues, DMs that should be searchable, account-specific
notifications that today go nowhere). A self-hosted Zulip gives the
cluster a stable, searchable, archived thread of record without
adding an external SaaS dependency.

Authentik is already in the cluster and accounts are already
provisioned there; the natural shape is Zulip as a SAML client of
Authentik, joining the IdP's account model rather than standing up
its own.

## Sources

Grounded against the upstream Zulip docker-compose stack and
SAML-auth docs, plus the cluster's existing Authentik + OWUI
deployment:

- `ext/docker-zulip/` — the official docker compose deployment,
  cloned from `github.com/zulip/docker-zulip`. The compose stack
  (`compose.yaml`) brings Zulip + bundled postgres / memcached /
  rabbitmq / redis. The data-volume layout under `/data` is
  documented in `ext/docker-zulip/docs/reference/data-volume.md`;
  the env-var surface for SAML / auth is in
  `ext/docker-zulip/docs/reference/environment-vars.md` and
  `ext/docker-zulip/docs/how-to/compose-authentication.md`.
- `ext/zulip-server/docs/production/authentication-methods.md` —
  the SAML section specifies the Python-social-auth backend, the
  `SOCIAL_AUTH_SAML_ENABLED_IDPS` dict, and the auth attribute names
  Authentik needs to send (`attr_user_permanent_id`,
  `attr_full_name`, `attr_username`, `attr_email` all set to the
  `…claims/emailaddress` / `…claims/name` schemas).
- The Authentik Zulip integration guide at
  `integrations.goauthentik.io/chat-communication-collaboration/zulip/`
  documents the property mappings + provider shape and recommends
  the IdP name `authentik` (which determines the cert filename on
  the Zulip side: `/etc/zulip/saml/idps/authentik.crt`).
- Existing install patterns: `config/ansible/app/install-authentik.yml`,
  `config/ansible/app/install-open-webui.yml`,
  `config/ansible/app/files/authentik-manager`, and
  `config/ansible/app/build-open-webui.yml`. The `authentik-manager`
  generates the OWUI OIDC provider and the forward-auth blueprints at
  runtime from `/cluster/https/domain`.
- Verified against current Zulip source: `ext/zulip-server/zproject/backends.py`
  shows `SocialAuthMixin.get_authenticated_user` resolves accounts by
  email (`common_get_active_user(email, realm)`), which is what makes
  the per-account SAML binding non-blocking (see "Account migration").

## Background

### Storage leader layout today

The storage leader (whoever mounts `/rbd/misc` and `/rbd/user` — see
the leader-election section of `AGENTS.md`) hosts all the apps that
need persistent state:

- `/rbd/user/postgres` — native Postgres, one cluster-wide instance.
  Each app gets its own DB + role, provisioned by the install
  playbook's `postgresql_db` / `postgresql_user` / `postgresql_owner`
  tasks. Authentik's `authentik` DB and Open-WebUI's `openwebui` DB
  both live here; the daily `backup-databases` script
  (`config/ansible/storage/scripts/backup-databases`) runs `pg_dumpall`
  over the cluster's Postgres, so every DB (including the future
  `zulip` one) is covered with no new wiring.
- `/rbd/misc/<app>/` — per-app scratch / data. Authentik puts
  `/media`, `/custom-templates`, `/certs` here; Open-WebUI puts its
  container data dir here; both are bind-mounted from docker compose.

All these apps run under `ycluster-apps.target`, so they follow the
leader — start/stop tracks the storage VIP. Authentik has a
manager-script (the `authentik-manager` we saw above) that owns
container lifecycle; Open-WebUI has the parallel
`open-webui-manager`. The install playbook installs the systemd unit
+ manager script + docker-compose.yaml + per-app systemd dep on
`misc-rbd.service` (the leader-only mount unit).

### Authentik today

`config/ansible/app/install-authentik.yml` +
`config/ansible/app/files/authentik-manager` set up:

- Authentik server + worker + redis containers on the storage leader.
- Postgres DB `authentik` on the cluster's native Postgres.
- Blueprints for the enrollment flow (invitation-gated), the
  recovery flow (admin-issued password-reset links), and the
  branding.
- Two domain-dependent blueprints generated at startup by
  `authentik-manager.write_owui_blueprint` and
  `.write_admin_blueprint`: the OIDC provider for Open-WebUI and the
  two proxy providers (forward-auth) for `admin.<domain>` and
  `app.<domain>`. These read `/cluster/https/domain` from etcd and
  re-render on every container start, so the external domain stays a
  single source of truth.

The Zulip SAML provider has the same shape as the OWUI OIDC provider
(dynamic, depends on `/cluster/https/domain`), so the natural place
for it is the same `authentik-manager` script — a third
`write_zulip_blueprint` function.

## Deployment shape

### What runs on the storage leader

Four services (one app container + three supporting containers), owned
by a new `zulip.service` unit (sibling of `authentik.service` /
`open-webui.service`), running under `ycluster-apps.target`:

- `zulip` — the Django + Tornado app, built locally from source
  (see "Build" below). Container runs as root; the entrypoint chowns
  `/data/uploads` to uid 1000 and manages `/etc/zulip` under `/data`
  when `LINK_SETTINGS_TO_DATA=True`.
- `memcached`, `rabbitmq`, `redis` — supporting services, kept from
  the upstream compose. Memcached is a cache (no persistent state);
  rabbitmq and redis keep ephemeral queues / pubsub state, and
  Zulip tolerates losing them on restart (queues replay from the DB
  on next delivery, per Zulip's standard recovery story).
- **No `database` service.** The cluster's native Postgres replaces
  Zulip's bundled `zulip-postgresql:14` — the compose drops the
  `database` service and points `SETTING_REMOTE_POSTGRES_HOST` at
  `host.docker.internal` (mirroring how Authentik and Open-WebUI
  reach the cluster Postgres from containers).

### Network isolation (applying the MinerU sidecar pattern)

MinerU runs on `--network=none` with a Unix-domain-socket + host
`socat` bridge so a parser fed untrusted PDFs can never make an
outbound `AF_INET` call (`docs/design/mineru-podman.md`). That
pattern is load-bearing for MinerU specifically: its whole job is to
consume attacker-influenced documents whose libraries can trigger
external fetches. Zulip is a different threat model, and the full
`--network=none` form does **not** fit:

- Zulip is a multi-container stack, not a single self-contained
  service. The `zulip` container must reach the **cluster's shared
  Postgres** (host network, `host.docker.internal`) in addition to
  memcached / rabbitmq / redis. Under `--network=none` it would lose
  all of those, and you'd have to `socat`-bridge the UDS against
  web + Tornado + postgres + memcached + rabbitmq + redis — a
  per-connection forwarder interposed on the DB write path that
  carries credentials and is latency-sensitive on every page load.
  That diverges from the upstream compose we deliberately keep, for
  little blast-radius gain on a trusted first-party app.
- The supporting containers (memcached / rabbitmq / redis) already
  expose no host ports — they are reachable only inside the stack's
  own bridge network, i.e. they already match the "no external
  interface" property the `--network=none` trick exists to create.

What we **do** adopt from the pattern (the parts that actually
reduce Zulip's reach):

- **No published host-wide ports.** The upstream compose publishes
  25 / 80 / 443 to every interface. Our compose publishes *only* the
  app's internal web port, bound to loopback (`127.0.0.1`), where
  host-side nginx — and certbot-manager's frp PROXY listener on
  `127.0.0.2:17443` — is the single controlled ingress. That is the
  same "incoming-only via a host sidecar" shape MinerU uses, minus
  the UDS. Port 25 stays unpublished (outbound SMTP and inbound
  email both off) and 443 is not published to the host
  (nginx terminates TLS).
- **Limit outgoing, not just ingress.** The `zulip` container's only
  required egress is the shared Postgres; there is no internet or
  cluster-service path in the current design (no SMTP, no push
  relay; SAML is inbound — Zulip never fetches the IdP
  server-side). So instead of `--network=none`, we enforce that
  single egress with a **host-side egress filter** — which is what
  `--network=none` reduces to for a container that still needs TCP
  to Postgres:
  - Pin the Zulip compose network to a fixed IPAM subnet in the
    compose file (docker otherwise hands the bridge a random
    172.x.y.0/24 per deployment, which the filter can't reference).
  - In the `DOCKER-USER` iptables chain (docker's preserved,
    non-flushed hook — the repo already manages iptables via
    `iptables-persistent`, see `setup-gateway-vip.yml`), allow the
    Zulip bridge subnet to reach only (a) itself — memcached /
    rabbitmq / redis — and (b) the leader's Postgres
    (`172.0.0.0/8` tcp/5432, matching the existing pg_hba scope);
    **DROP** all other egress. The supporting containers already
    publish nothing, so nothing else leaves the bridge.
  - Applied **unconditionally on every storage node** — no
    leader-election gating. The rule is source-scoped to the pinned
    Zulip subnet, so on any node where the Zulip stack isn't running
    that subnet is empty and the DROP matches nothing; it never
    touches other containers' traffic. `DOCKER-USER` stays active
    whenever dockerd runs, and only packets sourced from
    `172.z.z.0/24` are subject to it. The pinned subnet should not
    be reused by another stack; once the Zulip network exists,
    docker's per-daemon subnet-conflict check keeps a second network
    from claiming it. Per `AGENTS.md` it is still a firewall change,
    so it ships in the playbook and gets operator sign-off before
    prod. Fallback if a firewall change is vetoed: rely on "no
    published ports + loopback-only ingress" alone (weaker: a
    compromised container could still reach the cluster/172 subnet
    from inside).

If an SSRF-specific concern about Zulip's server-side image fetching
(thumbnailer / camo-style proxy for remote images in messages) ever
becomes load-bearing, address it at the Zulip application layer
(restrict or disable the image proxy), not by widening the network
filter. Decision: keep the docker-compose bridge + loopback-only
published web port + nginx/socat ingress, and add the `DOCKER-USER`
egress filter (pinned subnet → Postgres-only) as the outgoing
control. Do **not** port `--network=none` + UDS to the Zulip app
container.

### RBD layout

| Path | Purpose | Source |
| --- | --- | --- |
| `/rbd/misc/zulip/data` | bind-mounted onto the Zulip container's `/data` — holds uploads, the auto-backup `pg_dump` artifacts, and (with `LINK_SETTINGS_TO_DATA=True`) `/etc/zulip/`. Owned by uid 1000 (the image's `zulip` user); the entrypoint chowns it. | docker-zulip `data-volume.md` |
| `/rbd/misc/zulip/rabbitmq` | bind-mounted onto rabbitmq's `/var/lib/rabbitmq`. Owned by the `rabbitmq` container user (uid 999 in `rabbitmq:4.2`). | docker compose `rabbitmq:/var/lib/rabbitmq` |
| `/rbd/misc/zulip/redis` | bind-mounted onto redis's `/data`. Owned by the `redis` container user (uid 999 in `redis:alpine`). | docker compose `redis:/data` |
| `/rbd/misc/zulip/saml/idps/authentik.crt` | the IdP public certificate Zulip validates incoming SAMLResponses against. Fetched by `zulip-manager` (via the Authentik API) on every start, so it follows the RBD on failover. On a fresh install it's available as soon as Authentik is up. | Zulip auth-methods.md |
| `/rbd/user/postgres` (existing) | cluster-wide Postgres; gains the `zulip` DB + role. | already exists |

The user-volume setup is unchanged — Zulip's DB lives inside the
cluster's Postgres the same way Authentik's and Open-WebUI's do, so
`backup-databases` covers it without new wiring.

### Why not keep the bundled postgres

The official compose bundles `zulip-postgresql:14` because most
operators have no Postgres at all. The cluster already runs Postgres
16 on the leader, with daily dumps via `backup-databases` and a
hold-managed upgrade story
(`config/ansible/storage/upgrade-held-packages.yml`).
Adding a second instance means:

- Two Postgres versions to track and upgrade separately.
- A second set of `pg_dump` + restore scripts, outside the cluster's
  pipeline.
- A second connection-pool / memory budget on the storage leader.

Using `SETTING_REMOTE_POSTGRES_HOST` (the official Zulip knob for
this case — see `ext/zulip-server/docs/production/postgresql.md`)
gives us a single Postgres, free backups, and a coherent upgrade
path. The risk: a Zulip schema change incompatible with Postgres 16.
Mitigation: stage the upgrade in a throwaway instance on
`dev/cluster/` before prod, like every other cluster app's upgrade.

### Two wrinkles worth calling out before the install playbook ships

- **Collation**: Zulip's `create-db.sql` creates the DB with `C.UTF-8`
  collation. The cluster's `env.sh` sets `LC_ALL=C.UTF-8` (so
  `initdb` runs under that locale), but `postgresql_db` doesn't take
  a collation argument — it inherits the cluster default. Verify on
  the leader that `SHOW LC_COLLATE` reports `C.UTF-8` before
  creating the Zulip DB; if it doesn't, `CREATE DATABASE zulip
  LC_COLLATE = 'C.UTF-8' LC_CTYPE = 'C.UTF-8' TEMPLATE template0`
  must be run by hand before the playbook's `postgresql_db` task,
  or the Zulip migrations will fail with a collation-mismatch error.
- **SSL mode**: the leader's Postgres uses peer auth (the
  `postgres` Unix user). The Zulip container connects as the
  `zulip` Postgres role, not as a Unix user, so peer auth doesn't
  apply — but the container also doesn't have a TLS cert. Two
  options: (a) configure md5/scram-sha-256 password auth for the
  `zulip` role (the simplest, what Zulip's own `create-db.sql`
  assumes), or (b) extend the cluster's etcd-issued Postgres
  client-cert scheme to the Zulip container (more invasive).
  Default to (a).

### Why keep bundled memcached / rabbitmq / redis

Memcached is a cache with no durability. Rabbitmq and redis hold
ephemeral queues + pubsub; Zulip's queue processors recover from
the DB on restart, so a container restart drops no user-visible
messages (worst case: a delayed delivery, same as before). Bundling
them keeps the compose layout familiar and avoids cross-service
trust.

If Zulip's load grows to the point where dropping in-flight queue
state is unacceptable, the upgrade path is externalizing rabbitmq
to the cluster's future RabbitMQ-on-RBD (which doesn't exist yet —
only Postgres is a shared service).

## Build

The cluster's third-party-binary rule (`AGENTS.md`) forbids pulling
Zulip's published image from `ghcr.io`. Build locally instead, via a
`build-zulip.yml` playbook, vendored under `/opt/src/zulip-build/`,
pinned to the `12.1-0` release tag:

```yaml
# config/ansible/app/build-zulip.yml (sketch)
- name: Build Zulip container image in-cluster
  # Run on localhost so import_playbook can resolve `hosts` statically;
  # each task delegates to the buildhost (s1, via group_vars) — same
  # shape as build-open-webui.yml.
  hosts: localhost
  gather_facts: false
  vars:
    zulip_repo: https://github.com/zulip/docker-zulip.git
    zulip_version: "12.1-0"   # docker-zulip release tag; pins ZULIP_GIT_REF=12.1
    zulip_image_name: registry.xc:5000/zulip
  tasks:
    - block:
        - name: Verify registry is ready for push operations
          uri:
            url: "http://registry.xc:5000/v2/_catalog"
            method: GET
            timeout: 10
        - name: Create build directory
          file:
            path: /opt/src/zulip-build/docker-zulip
            state: directory
            mode: '0755'
        - name: Check out docker-zulip at pinned tag
          git:
            repo: "{{ zulip_repo }}"
            dest: /opt/src/zulip-build/docker-zulip
            version: "{{ zulip_version }}"
            force: yes
        - name: Build and push with buildx using registry cache
          shell: |
            docker buildx build \
              --build-arg http_proxy=http://10.0.0.254:3128 \
              --cache-from type=registry,ref={{ zulip_image_name }}:cache \
              --cache-to type=registry,ref={{ zulip_image_name }}:cache,mode=max \
              --progress plain \
              --tag {{ zulip_image_name }}:{{ zulip_version }} \
              --push \
              . | tee /tmp/build-zulip.log 2>&1
          args:
            chdir: /opt/src/zulip-build/docker-zulip
      delegate_to: "{{ buildhost }}"
```

The install playbook pulls `{{ zulip_image_name }}:{{ zulip_version }}`
from `registry.xc:5000`. Upgrade is a rebuild at a new tag + restart.
Pattern matches `config/ansible/app/build-open-webui.yml` and
`config/ansible/app/build-local-ai-proxy.yml`. Building the docker-zulip
Dockerfile runs `apt dist-upgrade`, `git clone`s the upstream Zulip repo
then `git checkout`s the pin (`ZULIP_GIT_REF=12.1` by default) — that
clone from github.com plus the apt update needs the cluster proxy, so
pass `--build-arg http_proxy=http://10.0.0.254:3128` like the OWUI
buildx args (the long pole in this step).

## Auth integration (Authentik SAML provider)

Three things have to line up: a SAML provider in Authentik, a cert
on the Zulip side, and matching attribute mappings.

### Authentik side

Add a third `write_zulip_blueprint` function to
`config/ansible/app/files/authentik-manager`, mirroring
`write_owui_blueprint`. It
generates a blueprint at service start from
`/cluster/https/domain`:

```yaml
# Generated ycluster-zulip.yaml
- model: authentik_providers_saml.samlprovider
  id: provider-zulip
  identifiers:
    name: authentik
  attrs:
    acs_url: https://zulip.$external_domain/complete/saml/
    sls_url: https://zulip.$external_domain/complete/saml/
    audience: https://zulip.$external_domain/
    issuer: https://auth.$external_domain/
    signing_kp: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
    verification_kp: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
    # Zulip (python-social-auth) posts the AuthnResponse to /complete/saml/
    # and requires the response to be signed, so use POST + sign both.
    sp_binding: POST
    sign_response: true
    sign_assertion: true
    property_mappings:
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/username]]
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/email]]
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/name]]
      # Managed `uid` mapping — emits the user pk (UUID) at
      # http://schemas.goauthentik.io/2021/02/saml/uid.
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/uid]]
- model: authentik_core.application
  identifiers:
    slug: zulip
  attrs:
    name: Zulip
    provider: !KeyOf provider-zulip
    meta_launch_url: https://zulip.$external_domain/
```

`attr_user_permanent_id` in Zulip's `SOCIAL_AUTH_SAML_ENABLED_IDPS`
points at Authentik's built-in managed **`uid`** mapping (user pk,
`http://schemas.goauthentik.io/2021/02/saml/uid`) rather than a custom
property mapping. Per Zulip's auth-methods.md, setting
`attr_user_permanent_id` to a value different from `attr_email`
activates automatic email-change sync — a user whose email is
renamed in Authentik keeps their Zulip identity and Zulip's email
updates on next login (matches the cluster's user-rename story in
`docs/design/user-rename.md`). Using the managed mapping avoids a
custom SAML property mapping and stays stable across Authentik
upgrades.

The IdP name in Authentik is `authentik`, which determines the cert
filename Zulip reads: `/etc/zulip/saml/idps/authentik.crt` — matching
the official integration guide. `zulip-manager` fetches that keypair's
public cert from the Authentik API on every start.

### Zulip side

Set these via docker env (`SETTING_*` prefix per
`ext/docker-zulip/docs/reference/environment-vars.md`):

```yaml
ZULIP_AUTH_BACKENDS: EmailAuthBackend,SAMLAuthBackend
SETTING_EXTERNAL_HOST: zulip.<domain>
SETTING_AUTHENTICATION_BACKENDS: |
  "zproject.backends.EmailAuthBackend",
  "zproject.backends.SAMLAuthBackend",
SOCIAL_AUTH_SAML_ENABLED_IDPS:
  authentik:
    entity_id: https://auth.<domain>/
    url: https://auth.<domain>/application/saml/zulip/sso/binding/redirect/
    # For email-change sync, attr_user_permanent_id must point at the
    # stable Authentik user pk (UUID) emitted by the
    # ycluster-zulip-user-permanent-id mapping, NOT at the email claim.
    # The exact claim URI is fixed when write_zulip_blueprint renders
    # that property mapping; here it is shown as a placeholder.
    attr_user_permanent_id: <permanent-id-claim-uri>
    attr_full_name: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name
    attr_username: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
    attr_email: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
```

Keep `EmailAuthBackend` enabled alongside SAML during bring-up, then
flip to SAML-only (`ZULIP_AUTH_BACKENDS=SAMLAuthBackend`) after the
operator's first SAML login, as a short optional safety window. Two
reasons to bootstrap with `EmailAuthBackend`:

1. The first Zulip org must be created with `EmailAuthBackend`. Zulip's
   docs say `EmailAuthBackend` "must be used for creating the initial
   realm and user" — that's the reliable bootstrap path. (A fresh
   install *can* also create the org through the SAML login flow per the
   same docs, but email-bootstrap is the predictable choice used here.)
   Once the realm exists, SAML authenticates the owner too.
2. `EmailAuthBackend` is bootstrap-only / a fallback, **not** a
   per-user migration requirement. Zulip's
   `SocialAuthMixin.get_authenticated_user` resolves accounts by email
   (`common_get_active_user(email, realm)` in `zproject/backends.py`),
   so every invited account whose email matches their Authentik email
   can SAML-login on the first try — no prior per-account SAML binding
   or email login is needed. The `NameID`/`attr_user_permanent_id`
   binding only matters for email-change sync (see below).

The `attr_full_name`, `attr_username`, and `attr_email` names above
match what Authentik's default `Name`, `Username`, `Email` property
mappings emit. `attr_username` points at the email claim because
Authentik's username *is* the email for internal accounts (see
`authentik_manager.add_user`).

`attr_user_permanent_id` is the one that intentionally differs from
the default configuration. Zulip auth-methods.md § "Synchronizing
email addresses with SAML" says email-change sync activates when
this claim differs from `attr_email`. The official Authentik Zulip
guide points it at the email claim (so sync stays off); our blueprint
instead sends the stable Authentik user `pk` (UUID) via Authentik's
built-in managed `uid` property mapping (wired in through
`property_mappings` above and referenced as
`http://schemas.goauthentik.io/2021/02/saml/uid`), so a user renamed
in Authentik keeps their Zulip identity and Zulip's email updates on
their next SAML login — matching the user-rename story in
`docs/design/user-rename.md`.

### The cert

`zulip-manager` fetches the SAML provider's signing cert from the
Authentik API on *every* start and writes it to
`/rbd/misc/zulip/saml/idps/authentik.crt`, which docker compose binds
into the Zulip container at `/etc/zulip/saml/idps/authentik.crt`.
Authentik exposes the cert two ways: via the Admin UI (the "official"
path per the integrations docs), or via the keypair's
`view_certificate` download URL from
`GET /api/v3/crypto/certificatekeypairs/`. The manager uses the API
path — same authorization as `authentik_manager._api_token()`, with
the bootstrap token already in etcd — looking up the
`authentik Self-signed Certificate` keypair by name. The cert is on
RBD so it follows the leader (the RBD is mounted only on the leader,
same as everything else under `/rbd/misc`). If Authentik is
unreachable the manager writes an empty placeholder (keeping the
bind mount resolvable) and Zulip starts email-only; SAML returns on
the next start.

## External exposure

Add a `zulip.<domain>` vhost, following the existing
`app.<domain>` template-based pattern. The host-side nginx is the
single controlled ingress — the Zulip stack publishes no host-wide
ports (see "Network isolation" under Deployment shape):

- A Jinja2 template at
  `config/ansible/app/files/zulip.conf.j2` (mirroring
  `config/ansible/app/files/open-webui.conf.j2`): a plain `:443`/`:80`
  server plus a PROXY-protocol listener on `127.0.0.2:17443` that the
  frp server lands on, rendered via certbot-manager `update-nginx`,
  with the shared locations include. The `(( port ))` marker is
  substituted with the Zulip container's internal port.
- `install-zulip.yml` drops the template into
  `/etc/nginx/templates/zulip.conf.j2` and runs
  `certbot-manager update-nginx` (same invocation as
  `install-open-webui.yml`); the manager renders the vhost for each
  domain it knows about and creates the Let's Encrypt cert on first
  run.
- An nginx sites-enabled symlink to the rendered
  `/etc/nginx/sites-available/zulip` (same `notify: restart nginx`
  pattern as the OWUI site).
- The storage VIP (`10.0.0.100`) already resolves `zulip.xc` once
  the DNS record is added to the cluster's DNS. In-cluster DNS is
  `ycluster dhcp update-hosts` (etcd → dnsmasq), *not* a `ycluster
  dns` command — see `docs/design/frp-migration.md` for how the
  current app vhosts register. The external `zulip.<domain>`
  A/CNAME is handled out-of-band with the domain provider.

Internal access: `zulip.xc` → storage VIP → nginx → Zulip container.
External access: `zulip.<domain>` → frp PROXY landing on
`127.0.0.2:17443` → nginx → Zulip. Same frp frontend the rest of the
cluster uses (see `docs/design/frp-migration.md`); no new ingress.

## State and backups

- **Postgres**: the new `zulip` DB on the cluster's Postgres at
  `/rbd/user/postgres` is dumped daily by the existing
  `backup-databases` script (which already dumps `authentik`,
  `openwebui`, and any other DB on that cluster). No new code.
- **`/rbd/misc/zulip/data`**: user uploads + Zulip-side
  configuration + the `pg_dump` artifacts Zulip writes under
  `backups/`. Backed up the same way every other
  `/rbd/misc/<app>/` is backed up — RBD snapshots + the daily
  `backup-databases`-style dump of `/rbd/misc/`. Verify the exact
  mechanism in `config/ansible/storage/install-misc-storage.yml` and the
  disaster-recovery doc (`docs/operations/disaster-recovery.md`) before
  relying on it for the first Zulip backup.
- **`/rbd/misc/zulip/{rabbitmq,redis}`**: ephemeral. No backup
  story beyond what RBD gives us for free.
- **IdP cert** (`/rbd/misc/zulip/saml/idps/authentik.crt`): lives
  with the rest of `/rbd/misc/zulip/`. The cert rotates via the
  Authentik cert-rotation flow; the install playbook's
  `notify` handler should re-fetch and re-write on cert change.

## Outbound email

**Decision: no outbound SMTP.** Zulip supports this directly via
Django's email-backend override (see
`ext/zulip-server/docs/production/email.md` § "Logging outgoing email
to a file for prototyping"). The cluster already runs without SMTP
for the same reason — Authentik invitations are out-of-band
(see `docs/operations/users.md`), and there's no general-purpose
mail relay in the cluster.

### Implementation

In the Zulip docker-compose env (the `SETTING_*` prefix maps to
`settings.py` per `ext/docker-zulip/docs/reference/environment-vars.md`):

```yaml
SETTING_EMAIL_BACKEND: 'django.core.mail.backends.console.EmailBackend'
```

Do not set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, or
`email_password`. Django's SMTP backend defaults to localhost:25
when `EMAIL_BACKEND` isn't set, which silently hangs on every
outbound email — the explicit `console` backend sidesteps that.
Emails Zulip would have sent land in the container's stdout, picked
up by `docker logs` / `journalctl -u zulip` for debugging. Switch
to `filebased.EmailBackend` with `EMAIL_FILE_PATH` pointing at
`/var/log/zulip/emails/` (a bind mount onto
`/rbd/misc/zulip/logs`) if a longer-lived audit trail matters.

### What this disables, and the workaround for each

- **Invitations**: Zulip's admin "Invite users" form sends an email
  with the signup link. With no SMTP, the link isn't delivered
  automatically. Workaround: the admin copies the invite URL from
  the admin UI and delivers it out-of-band (Slack DM, signal, etc.)
  — exactly the pattern Authentik uses for its
  `ycluster user invite` URLs.
- **Mention / new-message notifications**: Zulip sends these via
  email by default. With no SMTP, they go to the console instead of
  the user's inbox. Workaround: configure users to rely on the web
  UI's notification badge (the default in-app behaviour); or, if
  email notifications turn out to be load-bearing, add SMTP later
  — the only thing that changes is one env var.
- **Password resets**: irrelevant once `ZULIP_AUTH_BACKENDS` is
  reduced to `SAMLAuthBackend` (SAML-only). SAML handles auth
  via the IdP, which has its own recovery flow (Authentik's
  `ycluster user recovery` issues a reset link).
- **Email-address verification on signup**: Zulip normally sends a
  confirmation email when a new account is created. With SAML,
  the email is already verified by Authentik
  (`user.email` is the verified identity, and the SAML response
  carries it), so Zulip treats the new account as verified without
  needing the email round trip. No workaround needed.
- **Stream-invitation emails**: same as user invitations — admin
  copies the URL, hands it off out-of-band.

### What still works

- **Inbound email to streams** (Zulip's email-gateway integration):
  independent of outbound SMTP. The upstream compose publishes port
  25 (`app_protocol: smtp`), but our compose remaps the app's
  published ports to internal-only ports behind nginx (see "External
  exposure"), so port 25 is *not* exposed on the host; inbound email
  handling stays off until someone explicitly wants
  `EMAIL_GATEWAY_PATTERN` + a published SMTP port. Safe default.
- **Desktop / mobile push notifications**: Zulip's mobile apps use
  Zulip's own push-notification relay, not SMTP. Out of scope for
  this design.

### Switching back on later

If a use case emerges that genuinely requires outbound email
(e.g. an external auditor who only reads email), the only changes
are: set `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` /
`EMAIL_HOST_USER` to an operator-provided transactional SMTP
service (Mailgun / SES / SendGrid), put the password in
`zulip-secrets.conf` (the docker entrypoint's
`SECRET_email_password` mapping handles this), unset
`SETTING_EMAIL_BACKEND` so Django falls back to its SMTP backend,
restart. No code changes.

## Account migration

There is no pre-existing Zulip — this is a green-field Zulip org,
not a migration from somewhere else. The flow on day one:

1. `install-zulip.yml` brings the stack up; SAML is enabled but
   `EmailAuthBackend` is still in `ZULIP_AUTH_BACKENDS`.
2. An operator opens `zulip.<domain>`, which (with `EmailAuthBackend`
   enabled) presents Zulip's first-run realm-creation page. They
   create the realm and the operator's realm-owner account with a
   new password. Once the realm exists, SAML authenticates the
   owner too.
3. An admin issues Zulip invitations to the existing Authentik
   accounts (`ycluster user list` shows who's in the IdP). Each
   invite URL is copied from the Zulip admin UI and delivered
   out-of-band (Slack DM etc.) — see "Outbound email" above.
4. Each invited user logs in via SAML. Zulip matches the invited
   account by email (`common_get_active_user(email, realm)`), so no
   prior per-user SAML binding is needed on their first login. The
   `NameID`/`attr_user_permanent_id` pairing only matters for
   email-change sync. Existing chat threads in Zulip start fresh.
5. Once the operator has validated their own SAML round trip, the
   playbook flips `ZULIP_AUTH_BACKENDS=SAMLAuthBackend` (one variable
   change in `install-zulip.yml` + restart), keeping
   `EmailAuthBackend` as a fallback until then. This is a short
   optional safety window — not a per-user migration soak.

No `ycluster user rename`-style cross-system write is needed —
Authentik owns identity, Zulip is a client. The existing
`docs/design/user-rename.md` story already covers email renames;
Zulip picks up the new email on the user's next SAML login.

## CLI surface

There's no `ycluster zulip` subcommand in scope. The interesting
operations are admin-style (issue invitations, change
`ZULIP_AUTH_BACKENDS`, rotate certs) and Zulip's own admin UI
covers them once the realm exists. If a CLI surface turns out to be
needed, mirror `ycluster user` (the `ycluster/cli/user.py` pattern):
thin wrappers around the Authentik API or a direct Zulip REST API
call, run on the leader, printing JSON for `admin-api` to consume.

## Implementation order

Estimates are focused working days, including cluster testing,
Ansible idempotency, and doc updates. Each step is independently
shippable; 1-3 don't depend on the others, so the image build and
the Authentik blueprint can land in parallel with the install
playbook.

1. `config/ansible/app/build-zulip.yml` — clone docker-zulip, pin the
   `12.1-0` tag, build + push to `registry.xc:5000`. Pure plumbing; the
   long pole is getting the Dockerfile's apt sources + upstream Zulip
   clone to play nice behind the cluster's network. — **0.5–1 d**.
2. `install-zulip.yml` skeleton — docker compose on the leader,
   RBD bind mounts, systemd unit, postgres DB provisioning. No
   SAML yet, no TLS. Test against
   `dev/cluster/app-dev.yml` first (it already provisions the
   same leader-election primitives as prod). — **1–2 d**.
3. Add `ZULIP_AUTH_BACKENDS=EmailAuthBackend` + certbot-manager
   vhost + external-domain DNS. Stand up a working Zulip reachable
   on `zulip.<domain>` with email/password login. — **1 d**.
4. Extend `authentik-manager` with `write_zulip_blueprint` +
   `ycluster-zulip-user-permanent-id` property mapping. Cert
   fetcher writes `/rbd/misc/zulip/saml/idps/authentik.crt`. SAML
   goes live with `EmailAuthBackend` still in as a fallback.
   — **1–1.5 d**.
5. Operator's first SAML login validates the round trip, then flip
   `ZULIP_AUTH_BACKENDS` to SAML-only (a short optional safety
   window, not a per-user migration soak). — **0.5 d**.
6. `docs/operations/zulip.md` — operational runbook: upgrade
   procedure (rebuild at new tag + restart, like the OWUI upgrade
   doc), `app:backup` invocation, restore from
   `/rbd/misc/zulip/data`, what to do when the leader changes
   (everything under `/rbd/misc` follows the leader — nothing new).
   — **0.5 d**, written alongside the install playbook.

Total ≈ **4.5–6 d**, dominated by step 2 (RBD layout + systemd +
docker compose on a leader that already runs four other storage-app
services) and step 4 (Authentik blueprint iteration against a live
SAML round trip).

## Open questions

- **SCIM**: Zulip supports SCIM for continuous account sync
  (deactivation, group membership changes), and Authentik has a
  built-in SCIM provider. Zulip's SCIM server endpoint lives at
  `/api/v1/scim/v2/`. The natural addition once SAML is live —
  but it requires a separate Authentik provider type and a Zulip
  API token, so it's a follow-up rather than part of this design.
  Decide after SAML has been in service for a week.
- **Single-org vs multi-org**: Zulip supports subdomains
  (`SOCIAL_AUTH_SUBDOMAIN`) for multi-tenant setups. The cluster
  runs one Zulip, so we use the root domain and don't need
  `RelayState` machinery. Revisit if the cluster ever hosts
  multiple Zulip orgs.
- **Mobile push notifications**: Zulip's mobile apps need a
  push-notification relay (`zulip-push` or APNs/FCM credentials).
  Out of scope for this design; revisit when someone asks for the
  mobile app.
- **Storage sizing**: Zulip's `/data/uploads/` grows unbounded
  with attachments. The `/rbd/misc` volume currently has ~30 GB
  free across the cluster. Decide whether to enable Zulip's S3
  backend (moves uploads out of RBD; needs an S3-compatible
  service the cluster doesn't have yet) or to monitor and resize
  `/rbd/misc` as needed. Defer — check actual usage after a month
  of operation.
