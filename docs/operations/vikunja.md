# Vikunja task manager

Vikunja is the cluster's optional task/project manager. It runs leader-gated
on the storage leader, like Authentik, Forgejo and Zulip.

**Status:** playbook, image pin and CVE manifest are repo-landed; not yet
deployed. The first deploy must run `install-authentik.yml` first so the
Vikunja OIDC provider and client secret exist.

Design boundaries:

- **Backend:** Go, official distroless image, pinned by digest and copied into
  `registry.xc`.
- **Metadata:** shared PostgreSQL, database and role `vikunja`.
- **Files:** local filesystem under `/rbd/user/vikunja/files`.
- **Identity:** Authentik OIDC at `tasks.<domain>`, SSO-only. Local password
  auth is off, so the login page has no password form; re-enable it
  temporarily to recover if the IdP is unavailable.
- **External URL:** `https://tasks.<domain>` (the DNS record is out of band).
- **Internal URL:** `http://vikunja.xc` on the storage VIP.
- **No edge forward-auth:** Vikunja is its own OIDC/API credential boundary.
  The external vhost is therefore a pre-auth surface and is tiered P0-U in
  `perimeter_software.yml`.

## Files and units

| Item | Path |
|---|---|
| Playbook | `config/ansible/app/install-vikunja.yml` |
| Compose | `/etc/vikunja/docker-compose.yml` |
| Manager | `/usr/local/lib/cluster/vikunja-manager` |
| Rendered config | `/etc/vikunja/config.yml` |
| Data | `/rbd/user/vikunja` |
| Unit | `vikunja.service` (`ycluster-apps.target`) |
| Internal vhost | `vikunja-internal` (`vikunja.xc`) |
| External vhost | `vikunja_tasks` (`tasks.<domain>`) |
| Secrets | etcd `/cluster/config/vikunja/*` and `/cluster/config/authentik/vikunja-client-secret` |

`vikunja-manager` writes `config.yml` at every start. It contains the
persistent `service.secret`, the Postgres password, and the Authentik OIDC
client configuration. Do not edit it by hand.

## Deploy

The external vhost has two ordering requirements:

- `tasks.<domain>` must resolve to the frontend public IP before the
  template goes live. `certbot-manager` derives the certificate SAN list
  from every `*_<instance>` template on the leader, so a renewal that cannot
  complete HTTP-01 for `tasks.<domain>` fails for the whole certificate, not
  just this vhost.
- Re-issue the certificate after the playbook. `update-nginx` only renders
  vhosts; it does not add SANs. Run `ycluster certbot obtain` on the storage
  leader (it needs the leader-gated frpc tunnel), or skip both steps with
  `-e vikunja_external=false`.

```bash
cd /etc/ansible
./run-playbook.sh app/install-authentik.yml --limit storage
./run-playbook.sh app/install-vikunja.yml --limit storage
# on the storage leader:
ycluster certbot obtain
```

`install-vikunja.yml` seeds `registry.xc:5000/vikunja:2.7.1-<digest>` from the
pinned upstream digest if it is missing, creates the Postgres database and
role, installs the vhosts, and starts the unit.

Optional overrides:

```bash
# Internal only: local break-glass auth, no OIDC, no tasks.<domain> vhost
./run-playbook.sh app/install-vikunja.yml --limit storage -e vikunja_external=false

# Enable CalDAV or public link sharing (both are off by default)
./run-playbook.sh app/install-vikunja.yml --limit storage \
  -e vikunja_caldav=true -e vikunja_link_sharing=true

# IdP outage: put the local password form back (SSO stays enabled)
./run-playbook.sh app/install-vikunja.yml --limit storage \
  -e vikunja_local_auth_enabled=true
```

Re-running the playbook is idempotent and refreshes compose, the manager,
vhosts, Postgres credentials and the service. It does not re-issue
certificates; only do that when a vhost is added or the domain changes.

## First login and IdP recovery

OIDC users are provisioned on first login. Vikunja creates OIDC users even
with local registration disabled, so the access boundary is the Authentik
application policy: any IdP user allowed to reach the `vikunja` provider can
create an account. Bind the provider to a group policy if not every
Authentik user should have access.

The default install is SSO-only (`auth.local.enabled: false`), so the login
page has no password form. The local break-glass account exists only while
local auth is enabled; on an existing install it is still in the database
but cannot sign in.

To recover during an IdP outage:

1. Redeploy with `-e vikunja_local_auth_enabled=true`.
2. Read the break-glass credentials on the storage leader:

```bash
systemctl show -p Environment vikunja.service | tr ' ' '\n' \
  | sed -n 's/^VIKUNJA_BREAKGLASS_USERNAME=//p'

set -a; . /etc/ycluster/etcd-client.env; set +a
etcdctl get /cluster/config/vikunja/breakglass-password --print-value-only
```

3. Fix the IdP, then redeploy with `-e vikunja_local_auth_enabled=false`.

The manager creates the account on start when local auth is on, so a fresh
SSO-only install gets one at the first recovery deploy. The etcd value is
whatever was generated at creation; a password changed in the UI leaves it
stale. To reset, delete `/cluster/config/vikunja/breakglass-password` and
restart the unit: the manager resets the existing account and stores the
new password.

The break-glass account is a normal Vikunja user. Instance-wide admin UI,
time tracking and audit logs are Vikunja Pro features and are not part of
this install.

## Health and logs

```bash
systemctl status vikunja.service
cd /etc/vikunja && docker compose -p vikunja ps
journalctl -u vikunja -n 200 --no-pager
cd /etc/vikunja && docker compose -p vikunja logs --tail=200 vikunja
curl -fsS http://127.0.0.1:3456/health
```

The container has a healthcheck using `/app/vikunja/vikunja healthcheck`.
Host nginx is the only web ingress; the API is published on loopback only.

## Security posture

Vikunja had a concentrated 2026 audit cycle: NVD lists 67 CVEs, all published
in 2026 (4 critical, 20 high, 42 medium, 1 low), covering cross-tenant
authorization, link-share, TOTP, rate-limit and resource-exhaustion issues.
None are in CISA KEV. The fixes land across upstream releases from 1.1.0 up
to 2.7.1; **2.7.1 is the minimum version** because it is the first release
that closes the whole series (the last item, CVE-2026-115507, is fixed there).
Do not run an older tag. Public link sharing and CalDAV are disabled by
default because they were repeated CVE surfaces, and the external vhost has
no Authentik forward-auth (Vikunja is its own credential boundary). Restrict
the Authentik application to a group if the task manager should not be
available to every IdP user.

The container manifest tracks Vikunja's dependency modules, not Vikunja
itself: `go version -m` reports the application module as `(devel)`, so the
osv-feed collector cannot match `code.vikunja.io/api` advisories (and the Go
entries for it are unreviewed, without fixed ranges, so adding the module
would report permanent findings). Track upstream releases and the Vikunja
CVE feed directly; the manifest covers the transitive module surface.

## Persistence and backups

- **Postgres:** database `vikunja` is included in the cluster `pg_dumpall`
  backups.
- **Files:** `/rbd/user/vikunja/files` contains attachments. It is on the
  primary RBD volume and currently has no off-cluster file backup, same as
  Forgejo state.
- **Redis:** not used (`keyvalue.type: memory`); losing it does not lose data.
- **Service secret:** `/cluster/config/vikunja/service-secret` in etcd. Losing
  it invalidates sessions and API tokens, but does not lose tasks.

A restore needs the Postgres dump plus the files directory. Test a restore
before treating the service as production.

## Upgrades

1. Resolve the new upstream tag and amd64 digest.
2. Update `vikunja_version` and `vikunja_upstream_image` in
   `app/install-vikunja.yml`; the registry tag derives from the digest.
3. Regenerate `config/ansible/monitoring/container-manifests/vikunja.manifest`
   from the new binary (see `docs/operations/packages.md`).
4. Deploy `app/install-vikunja.yml --limit storage`.
5. Run the osv-feed collector and confirm the new Go module surface.

Do not run an older tag. The 2026 CVE series covers cross-tenant
authorization, link-share, TOTP, rate-limit and resource-exhaustion issues;
2.7.1 is the first release that closes it.

## Troubleshooting

- **`tasks.<domain>` returns 502** — check `vikunja.service` and the
  container health; the vhost proxies to loopback `3456`.
- **OIDC button missing** — the external domain or
  `vikunja-client-secret` was unavailable when the manager last rendered
  `config.yml`. Confirm the etcd key exists, then restart the unit.
- **Break-glass login rejected** — local auth is off in the default
  SSO-only install. Redeploy with `-e vikunja_local_auth_enabled=true`.
- **OIDC callback mismatch** — Authentik must contain both
  `http://vikunja.xc/auth/openid/authentik` and
  `https://tasks.<domain>/auth/openid/authentik`. Re-run
  `install-authentik.yml` to rewrite the blueprint.
- **Database authentication failure** — compare
  `/cluster/config/vikunja/db-password` with the `vikunja` Postgres role;
  re-running the playbook reconciles both.
- **Attachments fail** — verify `/rbd/user/vikunja/files` is writable by
  uid/gid 1000. nginx allows 110M against Vikunja's 100MB cap so multipart
  overhead does not turn an at-limit upload into a 413; keep that headroom
  if either value changes.
- **TLS name mismatch on `tasks.<domain>`** — the certificate predates the
  vhost. Run `ycluster certbot obtain` on the storage leader; `update-nginx`
  does not add SANs.
