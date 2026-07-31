# Zulip on the cluster

## Status

**Draft — direction decided, nothing implemented.**

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
  `zulip.<domain>`, behind the same rathole/nginx/certbot pipeline as
  `app.<domain>` and `admin.<domain>`.

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
- Existing install patterns: `app/install-authentik.yml`,
  `app/install-open-webui.yml`, `app/files/authentik-manager`
  (the latter generates the OWUI OIDC provider and the
  forward-auth blueprints at runtime from
  `/cluster/https/domain`).

## Background

### Storage leader layout today

The storage leader (whoever mounts `/rbd/misc` and `/rbd/user` — see
the leader-election section of `AGENTS.md`) hosts all the apps that
need persistent state:

- `/rbd/user/postgres` — native Postgres, one cluster-wide instance.
  Each app gets its own DB + role, provisioned by the install
  playbook's `postgresql_db` / `postgresql_user` / `postgresql_owner`
  tasks. Authentik's `authentik` DB and Open-WebUI's `openwebui` DB
  both live here; the daily `backup-databases` script (`storage/`)
  dumps them all.
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

`app/install-authentik.yml` + `app/files/authentik-manager` set up:

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

Three containers, owned by a new `zulip.service` unit (sibling of
`authentik.service` / `open-webui.service`), running under
`ycluster-apps.target`:

- `zulip` — the Django + Tornado app, built locally from source
  (see "Build" below).
- `memcached`, `rabbitmq`, `redis` — bundled services from the
  upstream compose. Memcached is a cache (no persistent state);
  rabbitmq and redis keep ephemeral queues / pubsub state, and
  Zulip tolerates losing them on restart (queues replay from the DB
  on next delivery, per Zulip's standard recovery story).
- **No `database` service.** The cluster's native Postgres replaces
  Zulip's bundled `zulip-postgresql:14`.

### RBD layout

| Path | Purpose | Source |
| --- | --- | --- |
| `/rbd/misc/zulip/data` | bind-mounted onto the Zulip container's `/data` — holds uploads, the auto-backup `pg_dump` artifacts, and (with `LINK_SETTINGS_TO_DATA=True`) `/etc/zulip/`. | docker-zulip `data-volume.md` |
| `/rbd/misc/zulip/rabbitmq` | bind-mounted onto rabbitmq's `/var/lib/rabbitmq`. | docker compose `rabbitmq:/var/lib/rabbitmq` |
| `/rbd/misc/zulip/redis` | bind-mounted onto redis's `/data`. | docker compose `redis:/data` |
| `/rbd/misc/zulip/saml/idps/authentik.crt` | the IdP public certificate Zulip validates incoming SAMLResponses against. Written once by `install-zulip.yml` after pulling the cert from Authentik via its API. | Zulip auth-methods.md |
| `/rbd/user/postgres` (existing) | cluster-wide Postgres; gains the `zulip` DB + role. | already exists |

The user-volume setup is unchanged — Zulip's DB lives inside the
cluster's Postgres the same way Authentik's and Open-WebUI's do, so
`backup-databases` covers it without new wiring.

### Why not keep the bundled postgres

The official compose bundles `zulip-postgresql:14` because most
operators have no Postgres at all. The cluster already runs Postgres
16 on the leader, with daily dumps via `backup-databases` and a
hold-managed upgrade story (`storage/upgrade-held-packages.yml`).
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
pinned to a git SHA:

```yaml
# app/build-zulip.yml (sketch)
- name: Build Zulip container image in-cluster
  hosts: storage[0]
  become: yes
  vars:
    zulip_repo: https://github.com/zulip/docker-zulip.git
    zulip_version: "12.1-0"   # git tag / SHA
    zulip_image_name: registry.xc:5000/zulip
  tasks:
    - name: Clone docker-zulip at pinned SHA
      git:
        repo: "{{ zulip_repo }}"
        dest: /opt/src/zulip-build/docker-zulip
        version: "{{ zulip_version }}"
        force: yes
    - name: Build Zulip image
      command: docker build -t {{ zulip_image_name }}:{{ zulip_version }} .
      args:
        chdir: /opt/src/zulip-build/docker-zulip
    - name: Push to cluster registry
      command: docker push {{ zulip_image_name }}:{{ zulip_version }}
```

The install playbook pulls `{{ zulip_image_name }}:{{ zulip_version }}`
from `registry.xc:5000`. Upgrade is a rebuild at a new SHA + restart.
Pattern matches `app/build-open-webui.yml` and
`app/build-local-ai-proxy.yml`.

## Auth integration (Authentik SAML provider)

Three things have to line up: a SAML provider in Authentik, a cert
on the Zulip side, and matching attribute mappings.

### Authentik side

Add a third `write_zulip_blueprint` function to
`app/files/authentik-manager`, mirroring `write_owui_blueprint`. It
generates a blueprint at service start from
`/cluster/https/domain`:

```yaml
# Generated ycluster-zulip.yaml (sketch)
- model: authentik_providers_saml.samlprovider
  id: provider-zulip
  identifiers:
    name: authentik
  attrs:
    acs_url: https://zulip.$external_domain/complete/saml/
    sls_url: https://zulip.$external_domain/complete/saml/
    issuer: https://auth.$external_domain/
    sp_entity_id: https://zulip.$external_domain/
    signing_key: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
    verification_kp: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
    property_mappings:
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/username]]
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/email]]
      - !Find [authentik_providers_saml.samlpropertymapping, [managed, goauthentik.io/providers/saml/name]]
      - !Find [authentik_providers_saml.samlpropertymapping, [name, ycluster-zulip-user-permanent-id]]
- model: authentik_core.application
  identifiers:
    slug: zulip
  attrs:
    name: Zulip
    provider: !KeyOf provider-zulip
    meta_launch_url: https://zulip.$external_domain/
```

The `ycluster-zulip-user-permanent-id` property mapping is new —
it sends the user's `pk` (UUID) as `NameID` so
`attr_user_permanent_id` in Zulip's `SOCIAL_AUTH_SAML_ENABLED_IDPS`
points at a stable identifier. Per Zulip's auth-methods.md, setting
`attr_user_permanent_id` to a value different from `attr_email`
activates automatic email-change sync — a user whose email is
renamed in Authentik keeps their Zulip identity and Zulip's email
updates on next login (matches the cluster's user-rename story in
`docs/design/user-rename.md`).

The IdP name in Authentik is `authentik`, which determines the cert
filename Zulip reads: `/etc/zulip/saml/idps/authentik.crt` — matching
the official integration guide.

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
    attr_user_permanent_id: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
    attr_full_name: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name
    attr_username: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
    attr_email: http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress
```

Keep `EmailAuthBackend` enabled alongside SAML. Two reasons:

1. The first Zulip org must be created by logging in as a
   realm-owner with `EmailAuthBackend` — Zulip refuses to create the
   org via SAML on a fresh install (per Zulip auth-methods.md,
   `EmailAuthBackend` "must be used for creating the initial realm
   and user"). Once the realm exists, SAML can authenticate the
   owner too.
2. Disable after the realm is created (`ZULIP_AUTH_BACKENDS=SAMLAuthBackend`),
   same pattern as Open-WebUI's `ENABLE_LOGIN_FORM=True→False` soak
   (see `docs/design/vm-usage-scheduling-accounts.md`).

The SAML attribute names above match what Authentik emits when its
default `Username`, `Email`, `Name` property mappings are selected.
The `attr_user_permanent_id` claim needs to differ from `attr_email`
to activate email-change sync (Zulip auth-methods.md § "Synchronizing
email addresses with SAML"). Authentik's username *is* the email for
internal accounts (see `authentik_manager.add_user`), so we point
both at the email claim — that means email-change sync *won't*
auto-activate. Real sync requires the property mapping above to
emit the authentik `pk` (UUID) under the
`…claims/emailaddress` namespace, which is what the sketch blueprint
does.

### The cert

The install playbook fetches the SAML provider's signing cert
once and writes it to `/rbd/misc/zulip/saml/idps/authentik.crt`,
which the docker compose binds into the Zulip container at
`/etc/zulip/saml/idps/authentik.crt`. Authentik exposes the cert
two ways: via the Admin UI (the "official" path per the
integrations docs), or via the provider's metadata XML returned
from `GET /api/v3/providers/saml/<pk>/` (the `<ds:X509Certificate>`
field). The install playbook should use the API path — same shape
as `authentik_manager._api_token()`, just hitting the providers
endpoint with the bootstrap token already in etcd. The cert is on
RBD so it follows the leader (the RBD is mounted only on the
leader, same as everything else under `/rbd/misc`).

## External exposure

Add a `zulip.<domain>` vhost, following the existing
`app.<domain>` template-based pattern:

- A Jinja2 template at
  `app/files/zulip.conf.j2` (mirroring
  `app/files/open-webui.conf.j2`) with the `(( port ))` marker
  substituted by the Zulip container's port.
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
  the DNS record is added to the cluster's DNS (a single
  `ycluster dns add` entry — see the DNS playbook).

Internal access: `zulip.xc` → storage VIP → nginx → Zulip container.
External access: `zulip.<domain>` → rathole tunnel → nginx → Zulip.
Same rathole frontend the rest of the cluster uses; no new ingress.

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
  mechanism in `storage/install-misc-storage.yml` and the disaster-
  recovery doc (`docs/operations/disaster-recovery.md`) before
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
  reduced to `SAMLAuthBackend` after the soak. SAML handles auth
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
  independent of outbound SMTP. The compose stack publishes port
  25 (`app_protocol: smtp` in the docker compose), so users can
  email `stream-name@zulip.<domain>` to post into a stream. The
  `EMAIL_GATEWAY_PATTERN` setting controls which addresses are
  accepted. This feature is opt-in — leave the setting blank and
  no inbound email handling happens, which is the safe default for
  our deployment until someone asks for it.
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
2. An operator logs in via `EmailAuthBackend` with the email +
   password they set in `app:init` (or via Zulip's first-run flow)
   to create the realm and the operator's realm-owner account.
3. An admin issues Zulip invitations to the existing Authentik
   accounts (`ycluster user list` shows who's in the IdP). Each
   invite URL is copied from the Zulip admin UI and delivered
   out-of-band (Slack DM etc.) — see "Outbound email" above.
4. Each invited user logs in via SAML; their Authentik account
   binds to the Zulip account via the `NameID`/`attr_user_permanent_id`
   pairing. Existing chat threads in Zulip start fresh.
5. After everyone is on SAML, the soak ends and the playbook
   flips `ZULIP_AUTH_BACKENDS=SAMLAuthBackend` (one variable
   change in `install-zulip.yml` + restart). Same pattern as the
   OWUI password-form soak.

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

1. `app/build-zulip.yml` — clone docker-zulip, pin a SHA, build +
   push to `registry.xc:5000`. Pure plumbing; the long pole is
   getting the Dockerfile's apt sources to play nice behind the
   cluster's network. — **0.5–1 d**.
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
   goes live behind the `EmailAuthBackend` soak. — **1–1.5 d**.
5. Soak (multi-day, in parallel with normal cluster use). Once
   everyone is on SAML, flip `ZULIP_AUTH_BACKENDS` to SAML-only.
   — **0.5 d** of work, plus the soak window.
6. `docs/operations/zulip.md` — operational runbook: upgrade
   procedure (rebuild at new SHA + restart, like the OWUI upgrade
   doc), `app:backup` invocation, restore from
   `/rbd/misc/zulip/data`, what to do when the leader changes
   (everything under `/rbd/misc` follows the leader — nothing new).
   — **0.5 d**, written alongside the install playbook.

Total ≈ **4.5–6.5 d**, dominated by step 2 (RBD layout + systemd +
docker compose on a leader that already runs three other apps) and
step 4 (Authentik blueprint iteration against a live SAML round
trip).

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
