# Zulip Operations

Zulip is the cluster's self-hosted chat/messaging server, running the
official `docker-zulip` compose stack (release `12.1-0`, Zulip 12.1)
built in-cluster from source. This is the operational runbook:
initial bootstrap, day-to-day operation, upgrade, and leader-change and
backup notes.

Design and decisions live in `docs/design/zulip.md`; the playbooks are
`config/ansible/app/build-zulip.yml` (image build) and
`config/ansible/app/install-zulip.yml` (deploy).

## Layout recap

The stack runs on the storage leader (where `/rbd/misc` is mounted).
Everything lives under `/etc/zulip`:

| Component | Detail |
|-----------|--------|
| Compose project | `zulip` (`/etc/zulip/docker-compose.yml`) |
| Web container | `registry.xc:5000/zulip:12.1-0` |
| Web publish | `127.0.0.1:8484` → container :80 (loopback only; host nginx is the sole ingress) |
| Bundled services | `memcached`, `rabbitmq`, `redis` (ephemeral, stack-private network) |
| Postgres | shared cluster Postgres, database + role `zulip` |
| Data dirs | `/rbd/misc/zulip/{data,rabbitmq,redis}` |
| systemd | `zulip.service` under `ycluster-apps.target`; `ExecStart=/usr/local/lib/cluster/zulip-manager start` |
| Secrets | etcd keys under `/cluster/config/zulip/*` (written by `zulip-manager ensure-secrets`) |
| Egress filter | host `ZULIP-EGRESS` chain (jumped from `DOCKER-USER`) pins the stack subnet `172.16.0.0/24` to self + Postgres + `push.zulipchat.com:443`; refreshed by `zulip-egress-refresh.timer` |
| Container `/etc/hosts` | managed `/etc/zulip/push-hosts`, bind-mounted read-only at `/etc/hosts`; rewritten with base entries + current bouncer addresses by the same timer |

Commands below assume you have a shell on the storage leader, or an
entry host (`YC_SYNC_HOST`) that reaches it. Most live commands run on
the leader:

```bash
LEADER=$(ssh "$YC_SYNC_HOST" 'ycluster cluster leader')
ssh -J "$YC_SYNC_HOST" "$LEADER"
```

## Access

- **External (primary):** `zulip.<domain>` via the frp/nginx/certbot
  frontend. This is the hostname Zulip is configured with
  (`SETTING_EXTERNAL_HOST`), so it's what Django accepts.
- **Internal:** `zulip.xc` → storage VIP → nginx. ⚠️ Currently returns
  HTTP 400 because `zulip.xc` is not in Zulip's allowed hosts (only the
  external hostname is). Until that's resolved, use the external
  surface. See "Known limitations".
- **Raw on the leader:** `curl -H "Host: zulip.<domain>" http://127.0.0.1:8484/`
  proxies straight to the container (no nginx).

## Initial bootstrap (create the realm + owner)

The install brings the stack up with `EmailAuthBackend` enabled but no
organization yet — that's why the site shows "No organization found".
There is **no preset bootstrap user/password**; it's created during
this flow with a password chosen by the operator.

1. Confirm the stack is up and healthy on the leader:

   ```bash
   systemctl is-active zulip.service
   cd /etc/zulip && docker compose -p zulip ps
   ```

   Wait until the `zulip` container reports `healthy` (first boot runs
   DB migrations, which take a few minutes).

2. Open `https://zulip.<domain>/` in a browser. If external DNS isn't
   ready yet, tunnel to the leader and browse it directly:

   ```bash
   ssh -J "$YC_SYNC_HOST" "$LEADER" -L 8484:127.0.0.1:8484 -N
   # then open http://127.0.0.1:8484/ with Host: zulip.<domain>
   ```

3. The org-creation UI is gated: opening `zulip.<domain>/new/` shows
   "Organization creation link required". Zulip won't create the first
   realm straight from the form — you must mint a one-time realm-creation
   link first. Generate it on the leader (inside the `zulip` container):

   ```bash
   cd /etc/zulip && docker compose -p zulip exec -T zulip \
     su zulip -c '/home/zulip/deployments/current/manage.py generate_realm_creation_link'
   ```

   This prints a single-use `https://zulip.<domain>/new/<token>` link.
   (The container has no `python` on PATH; `su zulip -c manage.py` is the
   docker-zulip way to run management commands.)

4. Open that link and fill in the organization creation form:
   organization name/type/language and the operator's email (+ the
   owner's real name and a **new password**). In this build the
   realm-creation-link flow creates the org **and** the initial owner
   account directly in one pass — verified against the DB (the owner
   row appears with realm-Owner role at the same instant the realm is
   created) — so **no confirmation email is emitted and no console link
   is needed** for bootstrap.

5. Verify the org + owner landed (on the leader):

   ```bash
   sudo -u postgres psql -d zulip -c \
     "SELECT id, string_id, name FROM zulip.zerver_realm;"
   sudo -u postgres psql -d zulip -c \
     "SELECT id, full_name, email, role, is_active FROM zulip.zerver_userprofile ORDER BY id;"
   ```

   The realm shows up here, and the owner's row has `role = 100`
   (realm owner). If a confirmation email *is* ever emitted (e.g. when
   inviting additional users by email), outbound email uses Django's
   `console` backend (no SMTP — see `docs/design/zulip.md` "Outbound
   email"), so such links are printed to the `zulip` container's stdout
   instead of delivered:

   ```bash
   cd /etc/zulip && docker compose -p zulip logs zulip
   ```

6. Verify: revisit `zulip.<domain>` — it now shows the login page and
   accepts the owner credentials. The owner's same email will later
   authenticate via SAML too (identity is shared by email).

## Day-to-day

### Status

```bash
systemctl is-active zulip.service
cd /etc/zulip && docker compose -p zulip ps
ycluster cluster leader        # confirm which node owns the stack
```

### Logs

```bash
journalctl -u zulip
cd /etc/zulip && docker compose -p zulip logs --tail 200 <service>
```

`<service>` is one of `zulip`, `memcached`, `rabbitmq`, `redis`.
Zulip's outbound email (no SMTP) also lands in the `zulip` logs.

### Restart / re-apply config

`install-zulip.yml` is idempotent; re-running it syncs config, vhosts,
and the egress filter and (on the leader) restarts the service:

```bash
# on the entry host
cd /etc/ansible && ./run-playbook.sh app/install-zulip.yml --limit storage
```

Fresh installs override `zulip_auth_backends` with `-e` for the org
bootstrap run (no playbook edit), then re-run without the override for
steady state:

```bash
# first install: bootstrap the realm + owner (EmailAuthBackend required)
./run-playbook.sh app/install-zulip.yml --limit <node> \
  -e zulip_auth_backends='EmailAuthBackend'
# after the SAML round trip is validated: steady state (committed default)
./run-playbook.sh app/install-zulip.yml --limit <node>
```

### Users join (auto-join)

The realm is set to **auto-join**: `zulip_auto_join: true` in
`install-zulip.yml` turns off Zulip's `invite_required`, so any
Authentik SAML user whose email isn't in Zulip yet is **auto-registered
as a MEMBER on first login** — no invitation needed. Previously,
`invite_required` was on by default (only admins can invite), which made
a new SAML login bounce back to the login page and made Sign Up report
"an invitation is required"; this is the setting that blocked
unprivileged Authentik users from joining.

Re-running `install-zulip.yml` applies (or removes) it via a leader task
that is idempotent and gated on `zulip.service` being active (the realm
only exists after org bootstrap, so a truly fresh install skips it):

```bash
# steady state: SAML users auto-join as MEMBER (committed default)
./run-playbook.sh app/install-zulip.yml --limit storage
# invitation-only signup:
./run-playbook.sh app/install-zulip.yml --limit storage -e zulip_auto_join=false
```

Roles/default groups aside, a user still has to exist in Authentik to
log in at all (identity is owned by Authentik); admins can manage roles
/ deactivate accounts from the Zulip admin UI. If you need an admin to
also hold a non-admin, non-zulip Authentik account, they log in with
that account's email and land as a separate Zulip MEMBER.

## Upgrade

Mirror the Open-WebUI upgrade approach
(`docs/operations/open-webui-upgrade.md`):

1. Resolve the new `docker-zulip` release tag and the corresponding
   `ZULIP_GIT_REF` (Zulip server version).
2. Bump `zulip_version` / `ZULIP_GIT_REF` in `build-zulip.yml` and
   `zulip_version` in `install-zulip.yml`.
3. Rebuild the image in-cluster:
   `./run-playbook.sh app/build-zulip.yml`
4. Redeploy: `./run-playbook.sh app/install-zulip.yml --limit storage`.
   The leader's restart handler applies the new image.

## Backup / restore

- **Postgres DB `zulip`**: dumped daily by the existing
  `backup-databases` script (same DB server as `authentik`, etc.).
- **`/rbd/misc/zulip/data`** (uploads + Zulip config + Zulip's own
  `pg_dump` artifacts): covered by the standard `/rbd/misc` RBD
  snapshot / daily backup path.
- **`/rbd/misc/zulip/{rabbitmq,redis}`**: ephemeral; not part of the
  backup story beyond RBD snapshots.

Verify the exact backup mechanism in
`config/ansible/storage/install-misc-storage.yml` and
`docs/operations/disaster-recovery.md` before relying on Zulip's backup.

## Leader change / failover

The stack runs only where `/rbd/misc` is mounted, and the unit
`Requires=misc-rbd.service`, so Zulip stops cleanly when a node loses
leadership and can't start on a non-leader. On a leadership change,
re-run the install playbook on the new leader so `/rbd/misc/zulip`
data, the Postgres DB/role, and the service are in place there.

## Configuration & auth lifecycle

- `ZULIP_AUTH_BACKENDS` is pinned by `zulip_auth_backends` in
  `install-zulip.yml`. The live steady state is `SAMLAuthBackend`
  (SAML-only). The flip is a **var, not a tag**, and it is overridable per
  run with `-e` (Ansible extra vars outrank play vars), so fresh installs
  need **no playbook code change**:
  - On a fresh install, run the playbook once with
    `-e zulip_auth_backends='EmailAuthBackend'` (or `''`) to create the
    realm + owner — Zulip requires `EmailAuthBackend` for the initial
    realm/owner. With `''`, zulip-manager auto-decides: it runs
    `EmailAuthBackend` until it can fetch the IdP signing cert from
    Authentik, at which point it adds the SAML backend.
  - After the operator's SAML round trip is validated, re-run the playbook
    with the committed `SAMLAuthBackend` default (no `-e`). Idempotent
    re-applies stay SAML-only because the default is committed.
- SAML wiring is split across the two managers, both repeatable from
  playbooks on a fresh install:
  - `authentik-manager write_zulip_blueprint` writes the Authentik
    `zulip` SAML provider + application blueprint
    (`/etc/authentik/blueprints/ycluster-zulip.yaml`), signed with the
    shared `authentik Self-signed Certificate` (`sp_binding: POST`,
    `sign_response: true`, `sign_assertion: true`). Attribute claims
    reuse Authentik's managed SAML mappings, including the `uid` mapping
    (user pk) for Zulip's `attr_user_permanent_id`.
  - `zulip-manager` fetches that keypair's public cert from the
    Authentik API and writes it to `/rbd/misc/zulip/saml/idps/authentik.crt`,
    which compose bind-mounts read-only into the container at
    `/opt/saml/authentik.crt` and references via
    `SOCIAL_AUTH_SAML_ENABLED_IDPS["authentik"]["x509cert_path"]`. The mount
    sits at `/opt/saml` (not under `/etc/zulip`) on purpose: docker-zulip's
    entrypoint runs `rm -rf /etc/zulip` when `LINK_SETTINGS_TO_DATA=True`,
    and a bind-mounted file under `/etc/zulip` blocks it with "Device or
    resource busy", crash-looping the container. If Authentik is
    unreachable it writes an empty placeholder and boots email-only; SAML
    returns on the next start.
  - Both sides derive all URLs from the single `/cluster/https/domain`
    value, so the Zulip-side `SOCIAL_AUTH_SAML_*` settings and the
    Authentik-side provider stay in sync.
- The live install is SAML-only: `zulip_auth_backends: "SAMLAuthBackend"`
  in `install-zulip.yml` writes `ZULIP_AUTH_BACKENDS=SAMLAuthBackend`
  on the unit, overriding the manager's auto-detection (this was flipped
  after the operator's SAML round trip was validated). Break-glass: flip
  back to `EmailAuthBackend,SAMLAuthBackend` if Authentik degrades —
  email accounts already exist, nothing is lost. Use `-e` for a one-off
  flip without editing the playbook, e.g.
  `-e zulip_auth_backends='EmailAuthBackend,SAMLAuthBackend'`;
  commit the new value only when it should become the persistent default.
- The egress filter (a dedicated `ZULIP-EGRESS` chain jumped from
  `DOCKER-USER`) is source-scoped to the Zulip subnet and inert
  anywhere the stack isn't running. It's gated by
  `zulip_egress_filter_enabled` in `install-zulip.yml` (a firewall
  change; set `false` to fall back to ingress-only if desired). The
  push bouncer hole is resolved dynamically and refreshed by
  `zulip-egress-refresh.timer` (every 15 min), so no IPs are pinned.
- DNS egress is DROPped too, so the same timer run that rebuilds the
  firewall also rewrites `/etc/zulip/push-hosts` (localhost +
  `host.docker.internal` via the docker0 host-gateway, then one
  `push.zulipchat.com` per current A record). The file is bind-mounted
  read-only at `/etc/hosts` in the `zulip` container, so bouncer IP
  rotation reaches the running container without a restart. If it looks
  wrong, run `/usr/local/lib/cluster/setup-zulip-egress` and
  `systemctl restart zulip`; making the compose change to drop the mount
  is the break-glass to fall back to docker's own `/etc/hosts`.

## Mobile push

Zulip's mobile apps deliver via Zulip's hosted push service, not SMTP:
the server registers with `push.zulipchat.com` and forwards device
tokens there. The playbook enables the client settings
(`ZULIP_SERVICE_PUSH_NOTIFICATIONS=True`,
`ZULIP_SERVICE_SUBMIT_USAGE_STATISTICS=False`, bouncer URL pinned) and
widens the egress hole to `push.zulipchat.com:443`; what remains is an
operator step because it accepts Zulip's ToS and manages the plan.

```bash
# on the storage leader, with the service running
/usr/local/lib/cluster/zulip-manager register-push
```

- `register-push` runs `manage.py register_server --agree-to-terms-of-service`
  inside the container, so accepting the ToS is baked into the step.
- Using `register-push` again after changing the contact email
  re-registers with the new contact.
- After registration, **every existing mobile user must log out of the
  app and log back in** so their device registers — push won't start
  until they do.
- `ZULIP_ADMINISTRATOR` (the `zulip_admin_email` var, set in
  `ycluster-private/group_vars/storage/zulip.yml`) is the contact the
  push service uses; it must stay a monitored mailbox.
- Plan: ≤10 users works on the Free plan; YCluster has more users, so
  the free **Community plan** (non-commercial org) is required. Apply
  on Zulip's side (separate from this repo); nothing in the playbook
  changes between plans.

## Known limitations

- **Internal `zulip.xc` returns 400** — not in Zulip's allowed hosts.
  Either add it to the allowed hosts or drop the internal vhost from
  the install playbook. External `zulip.<domain>` is unaffected.
- **No outbound email** (console backend): confirmation/invite links
  must be pulled from logs or admin UI and delivered out-of-band.
