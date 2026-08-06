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
| Egress filter | host `DOCKER-USER` chain pins the stack subnet `172.16.0.0/24` to self + Postgres only |

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

### Invite users

Users join via Authentik SAML. Before SAML is wired, the admin creates
accounts / invitations from the Zulip admin UI. With no SMTP the invite
email isn't delivered, so the admin copies the invite URL from the
admin UI and hands it off out-of-band (the same pattern as
`ycluster user invite` for Authentik). Invited accounts are matched by
email on first login.

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

- `ZULIP_AUTH_BACKENDS` is `EmailAuthBackend` today (bootstrap-only).
- Once the owner's SAML round trip is validated (design step 5), set it
  to `SAMLAuthBackend` in `install-zulip.yml` and re-run. Keep
  `EmailAuthBackend` as a fallback until then.
- The DOCKER-USER egress filter is source-scoped to the Zulip subnet
  and inert anywhere the stack isn't running. It's gated by
  `zulip_egress_filter_enabled` in `install-zulip.yml` (a firewall
  change; set `false` to fall back to ingress-only if desired).

## Known limitations

- **Internal `zulip.xc` returns 400** — not in Zulip's allowed hosts.
  Either add it to the allowed hosts or drop the internal vhost from
  the install playbook. External `zulip.<domain>` is unaffected.
- **No outbound email** (console backend): confirmation/invite links
  must be pulled from logs or admin UI and delivered out-of-band.
- **Mobile push** is not configured (Zulip's push-relay service).
