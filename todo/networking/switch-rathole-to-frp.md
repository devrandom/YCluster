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
