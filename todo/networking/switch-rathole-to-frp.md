# Switch rathole → frp

Rathole upstream is dormant — latest release is still v0.5.0 (2023-10-01),
last commit on `main` is 2025-07-29 (~12 months ago), ownership transferred
to @fernvenue/@sjtrny in 2025-06
([discussion #416](https://github.com/rathole-org/rathole/discussions/416))
with no further activity.
**[Issue #367](https://github.com/rathole-org/rathole/issues/367)** ("TCP
connection floods overwhelm the client") describes exactly the bug we hit
— CLOSE_WAIT / FIN_WAIT-2 socket accumulation — and has no merged fix.

It caused a real external HTTPS outage 2026-07-17 (~8 h flap; detail in
`../ycluster-private/incidents/dev1-rathole-flap-2026-07-17.md`).

**[frp](https://github.com/fatedier/frp)** is the replacement: actively
maintained (v0.70.0 shipped 2026-07-11), Go-based, supports the same use
case (TCP tunnels, token auth, hot-reload, per-service bind ports), and
includes PROXY protocol natively — so this migration also picks up the
standalone rathole-PROXY-protocol item for free.

## Scope (full rewrite)

Playbooks:
- `app/build-rathole.yml`
- `app/install-rathole.yml`
- `app/install-rathole-server.yml`
- `admin/install-vm-bastion.yml`

Configs:
- All `*client-config.toml.j2` / `*server.toml.j2` / start scripts / systemd
  units in `app/files/` and `app/templates/`.

Python helpers:
- `admin/files/ycluster/ycluster/utils/rathole_config.py`
- `ycluster/cli/rathole.py` — frp's config shape is different.

etcd schema:
- `/cluster/nodes/rathole/config` (token) and the analog frp key.

## Progress

- **frp installed**: dev cluster + prod frontends (`dev1a`/`dev1b`) + prod
  cores (`s1`–`s4`). Build from source at pinned SHA (`app/build-frp.yml`,
  AGENTS.md "No prebuilt third-party binaries").
- **Parallel run validated**: frp on alt ports `:17080`/`:17443`, rathole
  on `:443`/`:80`. End-to-end external traffic through frp exercised
  the full admin-api surface (public whitelist + authed locations +
  forward-auth via cluster IdP) plus open-webui + ycluster_app.
  Migration plan in `docs/design/frp-migration.md` deliverable #10.
- **Step 5 — port swap landed**: `a78a6845`. rathole moved off `:443`
  to `:14443` (rollback path), frp takes `:443` (production primary).
  Alt vhost on storage (`admin-api-alt.conf.j2`) unchanged — still
  listens on `127.0.0.2:17443` with `proxy_protocol`, frpc `localPort`
  stays `:17443`. Only `remotePort` changed `:17443`→`:443`.
- **Step 6a — public HTTP/HTTPS cutover (rathole `:80`/`:14443` ripped
  out)**: rathole `http`/`https` service blocks removed from
  `rathole-server.toml.j2` and the rathole client `http`/`https` blocks
  from `rathole-client-config.toml.j2`. frpc http `remotePort` is now
  `80` (was the `17080` alt), so frp serves the production primary
  `:80`/`:443`. rathole remains for ssh/admin/bastion ingress pending
  full step-6 cleanup. ACME HTTP-01 is now served via the frp alt vhost
  (`admin-api-alt.conf.j2` on `127.0.0.2:17080`) and `certbot_manager.py`
  gates on `frpc` instead of `rathole`. Stale ufw rules (`14443`
  rathole-https rollback, `17080` frp-alt http) get deleted at apply.
- **Step 6a — per-core SSH retired**: commit `1137963f`. frpc-ssh
  (`17200+N`) replaced the rathole 2200+N per-core tunnels; rathole client
  no longer registers them.
- **Step 6b — VM-bastion ingress switched to frp**: `admin/install-vm-bastion.yml`
  + `admin/templates/bastion-setup.sh.j2` now run an in-container
  `frpc-bastion.service` (`/usr/local/bin/frpc`) reading
  `/cluster/nodes/frp/config`, publishing each Incus host on its historical
  22NN port (derived in the playbook, same formula as the retired rathole
  template) so `ssh -J jump@<host>:<port>` is unchanged. The 22NN ports are
  allowed through ufw by `app/install-frp-server.yml`, and the matching
  vm-bastion blocks were removed from `rathole-server.toml.j2` (rathole now
  carries only admin 8821/8822).
- **Step 6 — pre-admin cleanup (no rathole dependency)**: `app.py`
  flipped `_rathole_public_host` → `_frp_public_host` (deliverable #8,
  VM-schedule page now reads `/cluster/nodes/frp/config`; same host value
  today, and it survives the rathole etcd-key deletion). Removed the
  retired rathole public vhost blocks (`127.0.0.2:443` whitelist +
  `127.0.0.2:80` redirect/acme) from `admin-api.conf.j2`; external public
  traffic stays on the frp alt vhost. Remaining step-6 items (per-client
  rate limits, rathole teardown/admin switchover) still open; the main
  `:443`/`:80` vhost stays non-PROXY (see step 6 below).
- **Step 6 — rip out rathole (pending, gated on soak)**: remove
  rathole playbooks/configs/units/helpers, switch rate-limit zones
  to per-client semantics, delete the old etcd key
  `/cluster/nodes/rathole/config`. PROXY protocol is **not** enabled
  on the main `:443`/`:80` vhost — well-known public ports must stay
  standard, because real HTTP clients and direct probes (blackbox,
  local health checks) don't speak PROXY. Instead the PROXY endpoints
  remain only on the loopback alt ports (`127.0.0.2:17080/17443`),
  which stay as the frp tunnel landing (`frpc` `localPort`), and the
  main vhost stays a plain non-PROXY listener so nginx can be hit
  directly / from inside the cluster. See
  `docs/design/frp-migration.md` "Cleanup" + deliverable #8
  (`_rathole_public_host` → `_frp_public_host` in `admin/files/app.py`).
  **SSH tunnels migrate in step 6 too**: there are two SSH surfaces
  and both need to move to frp without churning users:
  - **Published vm-bastion ports** (`0.0.0.0:22NN` per VM host —
    legacy 2211-2216 for idx 1-3, nvidia `2220+idx`, compute
    `2240+idx`, see `app/templates/rathole-server.toml.j2`): users
    have these in their `ssh -J jump@<host>:<port>` commands. Step 6
    binds these exact ports on frps so users don't have to update
    anything — `app/files/frpc.toml.j2` (or a new per-host
    frpc-vm-bastion variant) registers one tunnel per VM host with
    `remotePort` matching rathole's published port, frps binds it,
    and rathole's matching `vm_bastion_service` block goes away.
  - **Per-core SSH** (`127.0.0.1:2200+N` for rathole, `*:17200+N`
    for frp): internal loopback ports the bastion container's
    `rathole-bastion` client uses to forward user traffic into the
    cluster. Step 6 retires the rathole SSH services and rewrites
    `admin/install-vm-bastion.yml` +
    `admin/templates/bastion-setup.sh.j2` to use `frpc` instead of
    `rathole` (migration deliverable #9): the bastion container's
    client target moves from `localhost:220N` to `localhost:1720N`
    (or, to match rathole's old loopback port, frp moves to
    `2200+N` — but that requires a brief cutover window where
    neither binds). The in-container unit becomes
    `frpc-bastion.service`, and the pushed binary becomes
    `/usr/local/bin/frpc` (mirrors the existing rathole push dance).

- **Done — rathole rip-out (repo)**: rathole playbooks, configs, the
  `ycluster rathole` CLI + `rathole_config.py` helper, and the client is
  removed. `gateway_health.py` now reads `/cluster/nodes/frp/config` and
  probes the frp control port `:7000` (`parse_frp_port`), and the
  health-status rathole blocks are gone from `app.py`/`health-api.py`.
  The dev fixtures, system test, and docs (dependency-hygiene S8,
  frp-migration step 6) are updated.
- **Ready to deploy — prod uninstall**: rathole is dormant on the cluster
  (no service/process running) but the binary + `/etc/rathole` remain on
  the storage/core/compute/nvidia nodes. `app/uninstall-rathole.yml`
  removes them (plus any rathole systemd units and the
  `/usr/local/bin/rathole-config` wrapper via install-ycluster-package).
  Run with `--limit managed`, normal stop-before-deploy review.

## Soak window

Doc proposes minimum 4 weeks of idle parallel tunnels + 2 weeks of
prod traffic on the frp path, longer if Prometheus counters show
anomalies (FIN_WAIT-2 climbing, frp reconnect rate > N/h, latency
p99 regression vs rathole baseline) — `docs/design/frp-migration.md:213-217`.
Step 6 lands after.

## Plan

1. Stand up frp on the dev cluster first; one rathole service replaced at
   a time and monitored end-to-end.
2. Keep the public port layout identical (frontends keep
   `0.0.0.0:{http,https,vm-bastion-*}`).
3. Update `ycluster` CLI + etcd schema; keep backward compatibility during
   cutover.
4. Roll to prod: dev1a/b → bastions → s1–s3 rathole clients.
5. Remove rathole playbooks, configs, units, helpers, and the standalone
   rathole-PROXY-protocol item (folded in here).

Refs:
- `../ycluster-private/incidents/dev1-rathole-flap-2026-07-17.md`
- `wg-inbound-follow-leader.md` (the rathole PROXY half is folded in)

## Follow-up (recorded, not started)

- **Bastion: opaque "VM down" error.** During the nv4 smoke test, `ssh -J
  jump@<host>:<port> ubuntu@<vm>` to a stopped VM (`dr-g1`, powered off)
  failed with a generic `Stdio forwarding request failed: Session open
  refused by peer` / `Connection closed by remote host` — nothing indicates
  the target VM isn't running. OpenSSH's `-W`/direct-tcpip channel-open
  refusal carries no user-facing reason over the SSH transport, so a
  friendly message needs a client-side proxy helper or a bastion-side guard
  (e.g. per-VM health check) rather than relying on sshd's refusal text.
  Deferred; not part of this migration.

- **Roll out bastion migration to all of `compute`, not just known hosts.**
  `admin/install-vm-bastion.yml` self-prunes to Incus hosts (`incus_vm_host`
  / `incus_container_host`), so future or newly-registered VM/container hosts
  in the `compute`/`nvidia` groups won't be migrated by a `--limit`
  pointing only at the current bastions. To avoid stale rathole-bastion
  state piling up, eventually run it against all of `compute` (e.g.
  `--limit compute`) so any new Incus host is picked up automatically.
  2026-08-05 rollout covered: nv2, nv4, nv6, c1, c2, c3.
