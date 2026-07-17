# frp migration plan

Replace rathole with [frp](https://github.com/fatedier/frp) v0.70.0 across the
cluster. Rathole is dormant upstream ([discussion #416](https://github.com/rathole-org/rathole/discussions/416)),
caused a real external HTTPS outage 2026-07-17 (incident in
`ycluster-private/incidents/dev1-rathole-flap-2026-07-17.md`), and has no
PROXY protocol support — frp gets us all three.

Tracking item: `todo/networking/switch-rathole-to-frp.md`. The standalone
rathole-PROXY-protocol item is folded in here.

## Surface area to migrate

### Playbooks (rename `*rathole*` → `*frp*`)
- `app/build-rathole.yml` → `app/build-frp.yml`. frp is Go; the
  playbook clones `https://github.com/fatedier/frp.git` at a pinned
  SHA (declared in `group_vars/all/main.yml`), runs `make` (or
  `go build`), and lands the binaries at `/opt/src/frp/target/`.
  Mirrors the existing `build-rathole.yml` / `build-open-webui.yml`
  build-from-source pattern — see `AGENTS.md` "No prebuilt third-party
  binaries".
- `app/install-frp.yml` (core nodes, client)
- `app/install-frp-server.yml` (frontend nodes, server + ufw)
- `admin/install-vm-bastion.yml` → swap `rathole` for `frpc` everywhere;
  bastion unit becomes `frpc-bastion.service`

### Templates / files
- `app/templates/rathole-server.toml.j2` → `app/templates/frps.toml.j2`
- `app/files/rathole-client-config.toml.j2` → `app/files/frpc.toml.j2`
- `app/files/rathole-ssh-client-config.toml.j2` → `app/files/frpc-ssh.toml.j2`
- `app/files/rathole-client-start` → `app/files/frpc-start`
- `app/files/rathole-ssh-client-start` → `app/files/frpc-ssh-start`
- `app/files/rathole.service` → `app/files/frpc.service`
- `app/files/rathole-ssh.service` → `app/files/frpc-ssh.service`
- `admin/templates/bastion-setup.sh.j2` → render `frpc` config block +
  `frpc-bastion.service` unit

### Python helpers
- `admin/files/ycluster/ycluster/utils/rathole_config.py` →
  `frp_config.py`. Keep the same surface (`set/get/generate-client/
  generate-ssh-client/delete`) — callers don't need to change.
- `admin/files/ycluster/ycluster/cli/rathole.py` → `frp.py`. Same
  subcommands, renamed parser.
- `admin/files/ycluster/ycluster/utils/gateway_health.py` →
  `parse_rathole_port` → `parse_frp_port`; key stays `/cluster/nodes/frp/config`
  (see below) so the helper just reads a new key with the same shape.
- `admin/files/app.py` `_rathole_public_host` → `_frp_public_host`,
  same change.
- `admin/files/ycluster/tests/test_gateway_health.py` → rename
  `parse_rathole_port` tests; the underlying contract is unchanged.

### etcd schema

**Decision**: new key `/cluster/nodes/frp/config` with the same JSON shape
as `/cluster/nodes/rathole/config` (`{remote_addr, token}`). Reasoning:

- The shape `{remote_addr, token}` is already correct for frp — frp's
  `serverAddr` + `auth.token` are the same pair of fields.
- New key avoids race during cutover: rathole clients can keep reading
  the old key while frp is brought up; the CLI writes both during
  dual-stack window.
- All non-config consumers (gateway_health, app.py) flip to the new key
  atomically with the rathole shutdown.

Dual-stack window: during dev-cluster validation, the CLI writes both
keys. Once prod is fully on frp, the old key goes read-only and is
deleted in the cleanup commit.

## Config mapping (rathole TOML → frp TOML)

Same Jinja-rendered TOML; only the field names change. Public port layout
is preserved exactly (80/443, 2201-22NN, 8821/8822, 2210+idx / 2213+idx
legacy, 2220+idx / 2240+idx disjoint for idx > 3) so frontends, nginx, and
ufw rules need no changes.

Server (`frps.toml.j2`):
```toml
bindAddr = "0.0.0.0"
bindPort = 7000                       # was [server] bind_addr = ":2333"
auth.method = "token"
auth.token = "{{ rathole_token }}"
# PROXY protocol — folded-in item. Picks up client IPs so nginx
# rate-limits can drop back to per-client semantics.
transport.proxyProtocolVersion = "v2"

[[proxies]]
name = "http"
type = "tcp"
listenPort = 80                       # was bind_addr = "0.0.0.0:80"

[[proxies]]
name = "https"
type = "tcp"
listenPort = 443
# ... same for ssh{N}, admin1, admin2, vm-bastion-{c,nv}{idx}
```

Client (`frpc.toml.j2`):
```toml
serverAddr = "{{ remote_addr.split(':')[0] }}"
serverPort = {{ remote_addr.split(':')[1] }}
auth.method = "token"
auth.token = "{{ token }}"
transport.heartbeatInterval = 40
transport.heartbeatTimeout = 40

[[proxies]]
name = "http"
type = "tcp"
localIP = "127.0.0.2"
localPort = 80
# `transport.useProxyProtocol = true` once server has v2 enabled —
# mirrors the rathole 0.5.0 caveat that 127.0.0.1 stayed reserved.
```

The `idx` derivation (hostname `s<N>` → `ssh<N>` proxy name → port
`2200+N`) stays exactly the same; it's a Jinja-side concern.

## Parallel run strategy

Run frp alongside rathole for an extended soak period before any
cutover. Both daemons live on the same frontend and core nodes, on
**disjoint port ranges**, so neither can accidentally take the
other's traffic and rollback is `systemctl stop frp*` away.

**Why parallel over a phased cutover:**
- Real production traffic can hit frp end-to-end while rathole stays
  the production primary — zero blast radius for the frp deployment.
- PROXY protocol + per-client rate-limit semantics can be validated
  on the alt vhost from day one, without touching rathole traffic or
  the production rate-limit zones.
- Decouples "is frp correct?" from "do we still need rathole?" —
  two independent failure modes.

**Alt port allocation (frp):**
- `17080` / `17443` — alt http / https
- `17201`-`172NN` — alt ssh-per-core (N = core node idx)
- `17821` / `17822` — alt admin1 / admin2
- `17210`-`17216` — alt legacy vm-bastion (idx 1-3)
- `17220`-`17240` — alt nvidia vm-bastion (idx > 3)
- `17240`-`17260` — alt compute vm-bastion (idx > 3)

Same per-host derivation rules as `app/templates/rathole-server.toml.j2`
— only the base offsets change. The `idx` derivation (hostname
`s<N>` → `ssh<N>` proxy name → port `2200+N`) stays exactly the same.

**Soak plan:**
1. **Build + install frp** on dev cluster + prod frontends/core
   nodes. frp units `enabled=no` until step 2.
2. **Enable frp on dev cluster** — f1/s1/c1 with synthetic test
   traffic through alt ports. Add `admin-api-alt.conf` (alt vhost
   on `127.0.0.2:17443`, `real_ip_header proxy_protocol;` from day
   one). 1-2 weeks.
3. **Enable frp on prod frontends** — f1, f2, …. Still no real
   traffic; idle tunnels + `frpc status` + Prometheus scrape on
   `frp_*` counters. 1 week.
4. **Point dev1a/b primary traffic at frp** — external HTTPS for
   dev1a/b routed to `frps:17443` instead of rathole:443. Rathole
   still running but unused on those frontends. 1-2 weeks.
5. **Cut over remaining prod frontends** one at a time
   (dev1a/b → bastions → s1–s3 frpc). Each step is a single DNS /
   LB flip; rathole keeps running as the rollback target until step
   6.
6. **Cleanup commit** — gated on soak completion, not cutover
   success. Removes rathole playbooks, configs, helpers, the old
   etcd key, and `admin-api-alt.conf`.

**What this changes from the original cutover plan:**
- No "service-by-service" cutover on the same ports — both run on
  disjoint ports throughout the soak.
- PROXY protocol can be enabled on frp immediately (step 2) since
  it doesn't touch rathole traffic. The folded-in
  rathole-PROXY-protocol item gets validated early, in parallel
  with the current global-bucket behavior still serving prod.
- "Cleanup" is the explicit final milestone, gated on soak
  completion rather than cutover success.

## Deliverables, ordered

1. New `frps.toml.j2`, `frpc.toml.j2`, `frpc-ssh.toml.j2` templates
2. New systemd units: `frpc.service`, `frpc-ssh.service`, server inline
   in `install-frp-server.yml`
3. New `app/build-frp.yml` — clones fatedier/frp at the pinned SHA
   from `group_vars/all/main.yml`, builds via `make` (or `go build`),
   leaves binaries at `/opt/src/frp/bin/{frps,frpc}`
4. New install playbooks: `install-frp.yml`, `install-frp-server.yml`
   (fetch the build output from the buildhost, same `fetch:`/`copy:`
   pattern as `install-rathole.yml`)
5. New start wrappers: `frpc-start`, `frpc-ssh-start` (run the
   generate-config step then `exec frpc -c …`)
6. New `ycluster/utils/frp_config.py` + `ycluster/cli/frp.py`
7. `gateway_health.py`: `parse_frp_port` (read `/cluster/nodes/frp/config`)
8. `app.py`: `_frp_public_host`
9. `install-vm-bastion.yml` + `bastion-setup.sh.j2`: frpc instead of
   rathole; bastion unit renamed
10. PROXY protocol wiring on frps + alt nginx vhost (`admin-api-alt.conf`,
    `real_ip_header proxy_protocol;` from day one). Rathole-side public
    vhost and rate-limit zones are left untouched until step 6 cleanup.
11. Tests: rename `test_gateway_health.py` cases for `parse_frp_port`;
    add an integration smoke test under `dev/cluster/system-test.sh`
    that confirms external HTTPS through frps reaches the admin-api
    `public_status` endpoint on the alt port and returns 200.

## Open questions

- **Bastion client role.** The bastion container runs its own frpc;
  today rathole is in `/usr/local/bin/rathole` on the host and pushed
  into the container by `install-vm-bastion.yml`. Should the container
  run frpc from `/usr/local/bin/frpc` (mirrors today), or pull from the
  host via incus mount (avoids the binary-push dance)? Default to
  mirrors today — the checksum-gated push + atomic rename logic is
  already proven.
- **Soak duration.** "A while" needs a number to gate cleanup step 6.
  Proposal: minimum 4 weeks of step-3 idle tunnels + 2 weeks of step-4
  dev1a/b production traffic. Longer if Prometheus counters show
  anything anomalous (FIN_WAIT-2 climbing, frp reconnection rate > N/h,
  latency p99 regression vs rathole baseline).
- **Comparison metrics.** What's the side-by-side dashboard for the
  soak? Candidates: `frpc status` + `frps status` counters scraped by
  Prometheus; `ss -s` socket-state deltas per tunnel; nginx access log
  4xx/5xx rates on both vhosts; external HTTPS probe latency p50/p99
  on both ports. Need to define the alert thresholds for "stop the
  soak early".
- **Old-key readers.** Anyone outside the codebase reading
  `/cluster/nodes/rathole/config`? Grep should confirm before the
  cleanup commit; if found, coordinate the rename window.
- **Alt vhost location.** `admin-api-alt.conf` as a separate file
  (cleaner, removed wholesale at cleanup) vs a conditional block in
  the existing `admin-api.conf.j2` (less file churn, but cleanup
  needs surgical removal). Default: separate file.
