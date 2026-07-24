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
- **Step 6 — rip out rathole (pending, gated on soak)**: remove
  rathole playbooks/configs/units/helpers, drop the parallel-run
  artifacts (`admin-api-alt.conf.j2`, the location-body includes,
  the alt-port ufw rules, the frpc `remotePort=:443` registration
  which becomes the only one), enable PROXY protocol on the main
  `:443` vhost (now safe — no migration risk since frp is the only
  tunnel left), switch rate-limit zones to per-client semantics,
  delete the old etcd key `/cluster/nodes/rathole/config`. See
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
