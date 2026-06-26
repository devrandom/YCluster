# admin-api surface area on non-storage nodes

## Status

Open question — not implemented. Inventories what `admin-api` does on
each node type today and proposes splitting it so non-storage nodes
serve a small surface (two routes) instead of the full ~3100-line Flask
app.

**Headline.** Of 42 routes, exactly **2** must serve on every
`managed` node: `/api/health` (the cluster-status fan-out hits
every node) and `/metrics` (Prometheus scraping — VM-host compute
nodes need VM-service metrics; lite exposes a stripped subset off-
storage). On core, 4 more routes are needed (2 prom SD, 2 PXE
autoinstall — local-prom and PXE tftp are both `hosts: core`). The
remaining ~36 are leader-only after the structural moves in Phase
A. The lite profile is therefore small *by construction*.

## Background

`admin-api.service` is a single Flask app (~3100 lines, 42 routes,
listening on `127.0.0.1:12723`) deployed by `setup-web-services.yml`
on every `managed` host (storage + compute + nvidia + adhoc + nas).
nginx fronts it cluster-internal as `admin.<domain>`. External
traffic arrives via rathole → `127.0.0.2:443` on the **storage
leader**, where the authed public vhost runs.

### Cluster leadership

| Concept | Key / VIP | Holder | Role |
|---|---|---|---|
| Storage leader + VIP | `/cluster/leader/app` + `10.0.0.100` | one of s1-s3 (tied by keepalived `vrrp_script`) | rotates as one; full admin-api, Docker registry, external tunnel |
| DHCP leader | `/cluster/leader/dhcp` | one of s1-s3 | rotates; usually ≠ storage leader; PXE/tftp |
| Gateway VIP | `10.0.0.254` | one core | routing, DHCP-forwarding, DNS |

The storage leader and storage VIP are bound together by the
keepalived health check in `setup-storage-vip.yml` (`chk_storage_leader`
returns 0 only if the local hostname matches `/cluster/leader/app`),
so they always sit on the same node and move together. All three
are restricted to storage by ansible gating (`hosts: core` or
`hosts: storage`).

### Routes — what each needs

| Route | Caller | Non-storage consumer? |
|---|---|---|
| `/api/health` | `/api/cluster-status` fan-out (ThreadPoolExecutor to every node, `app.py:2193-2207`); per-node `cluster-heartbeat.py` | **Yes** — must serve on every `managed` node |
| `/metrics` | local-prometheus (VM-host compute targets in particular); node-level Prometheus on incus hosts | **Yes** — lite handler serves on every `managed` node; needed for VM-service metrics on incus hosts |
| `/api/prometheus/targets/<job>` | local-prometheus http_sd | core only |
| `/autoinstall/{meta-data,user-data}` | PXE GRUB (tftp is `hosts: core`) | core only |
| `/api/cluster-status`, `/api/ping`, `/api/time` | operator/probes (public vhost on leader) or localhost (squid self-check, storage-only) | leader-only |
| `/bootstrap/{macos,nas,nvidia}` + `/api/allocate` | LAN bootstrap → calls back to `/api/allocate` | leader-only after structural move (a) |
| `/bootstrap/wg*`, `/api/wg/*` | rathole (public vhost) | leader-only |
| `/admin/*`, `/api/inventory*`, `/api/{dhcp-config,hosts,drain,status,allocations,alert-webhook}` | operator browser or `update_dhcp_hosts.py` / dnsmasq (storage-only) | leader-only |
| `/static/*`, `/`, `/assets/*` | pages above | leader-only |

**Summary of the non-storage requirement:** two routes
(`/api/health` + `/metrics`) on every managed node; 4 more routes
on core; all others leader-only. The lite `/metrics` exposes a
stripped subset (dns, ntp, clock_skew, node_healthy, plus VM-service
gauges on incus hosts) — no etcd/ceph/leadership detail, since
those are constant off-storage.

### Phase A structural moves (no behavior change yet)

**(a) Bootstrap pages become leader-only.** Today `serve_bootstrap`
sets `API_SERVER = f"http://{request.host}"`. If we restrict
`/bootstrap/{macos,nas,nvidia}` to the leader, `API_SERVER` in the
rendered script becomes the storage VIP / leader URL by
construction, and `/api/allocate` (only ever called back from those
scripts) becomes a leader-only route. `wg`/`wg-macos` are already
leader-only via the public vhost.

**(b) Prom repointing is moot.** Local-prom is `hosts: core`, so
its SD URLs already collapse to core. No repointing needed.

### PXE follow-the-server fix

Today's wart: the GRUB config in `setup-pxe-boot.yml:140`
hardcodes `cloud-config-url=http://10.0.0.254/autoinstall/...`
(the gateway VIP), not the tftp server. DHCP Option 66 already
gives PXE clients the tftp server's IP; GRUB should use it
(`linux (http,$http_ip)/...`) so autoinstall fetches stay on the
DHCP leader (storage). Once that's fixed, the autoinstall routes
are DHCP-leader-only as expected.

### Cost of running the full process cluster-wide

- **Memory + deps:** ~50 MB of apt-installed Python (`python3-waitress`,
  `flask`, `etcd3`, `scapy`, `psycopg2`, `requests`). Same on every
  node whether it needs them or not.
- **Cold start:** 100–200 ms. Noticeable on low-end compute (nv1/nv2
  are Jetson Orin class).
- **Ceph identity cruft:** read-only `client.admin-api` is
  provisioned on every node, but `/etc/ceph/ceph.client.admin-api.keyring`
  is only installed on storage (`setup-web-services.yml:298-314`).
  Off-storage the user exists but no keyring — leftover from when
  ceph was checked elsewhere.
- **Boot-time etcd-wait loop:** already skipped off-core
  (`app.py:3092-3103`). We don't pay the wait cost; we do pay the
  import cost.
- **Net:** ~50 MB of Python packages + a Flask process on every
  non-storage node, all to serve 2 routes (`/api/health` + `/metrics`).

## Design 1 — single-process profile split

Keep `admin-api.service`, but `ExecStart` a different **mode** based
on node type. One Flask app; heavy routes only register if
`is_storage_node()`.

1. Service unit unchanged (same port, same user). No migration.
2. Route registration gated by node type. Off-storage blueprint
   registers `/api/health` and a stripped `/metrics` handler (no
   etcd/ceph/leadership detail — just dns/ntp/clock_skew + VM-
   service gauges). Optionally also `/api/ping` for liveness.
3. Structural move (a) — bootstrap pages leader-only, makes
   `/api/allocate` leader-only as a side effect.
4. Structural move (b) — moot (local-prom is core-only).
5. `install-ycluster-package.yml` and `setup-web-services.yml` keep
   `hosts: managed` — no ansible change.

**Wins:** off-storage is routes-thin; one process, one unit, one
log to read; no nginx change.

**Costs:**

- Conditional route registration is a small wart in `app.py`
  (e.g. `register_heavy_routes(app)` helper).
- **Off-storage still imports the heavy blueprint at module-load
  time** (the `if` skips registration, not imports). Lazy imports
  inside handlers mitigate this but are fragile.
- ~50 MB apt deps still installed on every node — we can't drop
  them without a service split.

### Liteness of the lite handlers

The two lite routes are not Flask hello-worlds — `get_comprehensive_health()`
at `app.py:1709` does real work on every `/api/health` call (and `/metrics`
shares the same handler):

- ~5 `systemctl is-active` subprocesses (postgresql, qdrant,
  dnsmasq, squid, gated-out leaders).
- ~5 TCP connects to local service ports (5432, 6333, 8067, 3128,
  4001).
- 1 dnspython resolution against `127.0.0.1` (DNS check).
- 1 NTP UDP query to `10.0.0.254` (clock-skew).
- 1 self-call to `http://127.0.0.1:12723/api/ping` (squid
  proxy-functional test at `app.py:1904`).

Off-storage many of these short-circuit (etcd/ceph/leader checks
return `not_applicable`), but the per-node service-port probes,
dnspython, NTP, and the self-call still run. So
non-storage `/api/health` + `/metrics` is ~10 cheap-but-real I/O
ops per call — tens of ms in the happy path, hundreds if a port
is timing out.

The DNS resolver check (`check_dns_status`) stays: useful local-
misconfig detector on every node, trivial cost (one dnspython UDP
query against 127.0.0.1). Off-storage, `ycluster_service_health`
entries for etcd/ceph/postgresql/qdrant/etc. resolve to constant
values (mostly `standby` or `not_applicable`); only `dns`, `ntp`,
`clock_skew`, and on incus hosts `vm_service` actually vary over
time.

## Design 2 — two-service split

Rename `admin-api` to the **full** service (storage-only) and
introduce `health-api.service` (the lite service, on every
`managed` node). Two units, two ports to keep routing obvious:

```
admin-api         :12723  storage-only  full 42 routes
health-api        :12724  managed-wide  /api/health + /metrics
```

The lite service serves **`/api/health` + `/metrics`** — the only
routes non-storage actually needs. We determined this by walking
the consumer graph: local-prom needs VM-service metrics on
VM-host compute (so `/metrics` belongs on every `managed` node),
and the cluster-status dashboard's fan-out needs `/api/health`.
It does not replicate `/autoinstall/*`, prometheus SD, or the
heavy `/metrics` handler. The lite is essentially a Flask hello-
world plus the comprehensive-health handler and a stripped
`/metrics` exposition — under 100 lines.

Plus:

- nginx `proxy_pass` per location chooses `:12723` (storage) or
  `:12724` (lite).
- The system user splits: `admin-api` (heavy, storage-only);
  `health-api` (lite, on every node).
- `setup-web-services.yml` becomes two plays: heavy in the storage
  play; lite in the managed play.
- New routes need a one-line `WHERE = 'light'|'heavy'` registry
  comment — easy to forget, needs code-review attention.

**Wins over Design 1:**

- **Explicit** surface. No "does this route work off-storage?"
  surprises — if it's in the lite service, it does; if not, it
  doesn't.
- Lite is genuinely tiny: under 100 lines, ~5 MB resident, ~50 ms
  cold start. Heavy app's 50 MB Python deps (`vmm`, `authentik_manager`,
  `psycopg2`, `scapy`, …) are not loaded on non-storage.
- Easier to harden separately: lite doesn't need `etcd-client`
  group membership (etcd is firewalled to storage), so the lite
  user has *no* etcd access by definition.

**Costs:**

- Two systemd units to keep healthy. Two restart policies. Two log
  streams. Slightly more `setup-web-services.yml` complexity.
- Two ports to remember. `12723` is in a lot of places already
  (Prometheus config, monitoring.yml, nginx configs, dev docker).
- Migration cost: every consumer of `:12723` needs review — a
  documentation/code-surfacing exercise that Design 1 mostly
  skips.
- Two loopback ports means two opens; loopback-only so security
  cost is zero, cognitive cost is not.

## Comparison

| Axis | Today | Design 1 (profile) | Design 2 (two-service) |
|---|---|---|---|
| Service units | 1 | 1 | 2 |
| Ports | 1 (`:12723`) | 1 (`:12723`) | 2 (`:12723`, `:12724`) |
| Per-node memory | ~50 MB | ~50 MB | ~5 MB lite + 0 MB heavy |
| Per-node Python deps | all of Flask + etcd3 + scapy | same (heavy imports loaded) | lite: Flask only; heavy: full |
| Per-node etcd access | yes | yes — heavy blueprint loaded, gated via `is_etcd_node()` | no — lite is etcd-free |
| Routes | 42 | 42; off-storage resolves 2 | 2 lite, 42 heavy on storage |
| Operator surprise | none | "where's the heavy page?" off-storage | none — routing explicit |
| Migration risk | — | low — single unit, conditional routes | medium — port + service split |
| Rollback | — | drop the `if`, all routes return | unit off; old unit on |

## Recommendation

**Design 2 — two-service split.** Non-storage needs `/api/health`
+ `/metrics` (the dashboard fan-out + Prometheus scraping for VM-
service metrics on incus hosts). No other route is hit off-
storage: local-prom needs are core-only otherwise, PXE tftp is
core-only. The lite is a small Flask app serving just those two
routes; splitting it out avoids loading the heavy blueprint's
Python deps on non-storage. For 13-host clusters the absolute
saving is modest (a few hundred MB cluster-wide), but the
cognitive win is bigger: off-storage admin-api becomes "an
endpoint that answers `/api/health` + `/metrics`", nothing more.

**Why not Design 1?** Conditional registration still loads the
heavy imports at module-init time. We could lazy-import inside
handlers, but it's fragile and the code stays "a single big app
with parts disabled" rather than "two clear services". For the
same dev work, Design 2 gives a smaller runtime and cleaner
separation.

## Phasing

1. **Today (no code change needed):** `is_etcd_node()` already
   gates etcd-client off non-storage (`app.py:1245-1253`). The
   remaining cruft is templates, per-node apt packages, and routes
   that return 503 off-core.

2. **Phase A — structural moves (no behavior change yet).**
   - Change `serve_bootstrap()` to render `API_SERVER` as the
     storage VIP / leader URL instead of `request.host`. Makes
     `/api/allocate` leader-only as a side effect.
   - Skip the prom repointing — local-prom is core-only, already
     gated by ansible.
   - PXE follow-the-server fix: repoint GRUB's `cloud-config-url`
     at the tftp server IP (Option 66) instead of `10.0.0.254`.
   - Verify in dev: PXE clients still bootstrap, dashboards still
     work.

3. **Phase B — split the lite service out.** Add
   `health-api.service` (a small Flask app serving `/api/health`
   + a stripped `/metrics`), install it `hosts: managed`. The
   existing `admin-api.service` becomes storage-only. nginx fronts
   the right port per location. Verify in dev, then prod.

## Open questions

- **Inventory collection.** `/api/inventory/hardware/<host>` GET
  is read-only. After Phase 1 of `etcd-access-hardening.md`, off-
  storage admin-api instances will return 503 on inventory reads
  the central collector relies on. **Need to verify the
  collection path resolves to the storage leader first.**
- **`/api/health` etcd access.** The leadership probe
  (`is_storage_leader` / `is_dhcp_leader` / etcd quorum) is
  technically optional off-storage (will always return "no"). But
  `check_etcd_status()` runs as part of health — verify it's
  acceptable to error out on etcd unreachable off-storage. Today
  `is_etcd_node()` skips the wait loop off-core; the client itself
  is probably already gated. Confirm.
- **Frontend `f*` nodes.** Don't run admin-api (`managed` excludes
  `frontend`). Confirm no caller hits `<frontend>.xc:12723` —
  review rathole config + forward-auth setup. If they do, we
  need a strategy.
- **`/autoinstall/user-data` requires etcd** for the allocation
  write. Two reads: (i) keep admin-api's etcd-client capability on
  non-storage just for this route — pragmatic, doesn't simplify
  the firewall story; (ii) delegate to the leader over a local
  socket — cleaner, but the socket becomes its own attack surface
  and PXE responses now require a round-trip. (ii) is probably
  net-negative for the small etcd-hygiene gain.
- **macOS nodes** are *consumers* of admin-api (the macOS nodes
  themselves don't run the systemd service). Already handled.

## Rollback

**Design 1** is *additive*. The conditional route gating is a
behavior change but every existing route still resolves somewhere
— just on storage rather than every node. Reverting is "drop the
`if is_storage_node():` guard, full route set on every node." No
data loss, no key migration, no config-file rollback.

**Design 2** is a *split*. Rolling back means bringing the full
service back on managed nodes and disabling `health-api` — a
one-line ansible change. Both designs are safe to trial on the dev
cluster (`dev/cluster/dev-cluster.sh reset`) before any prod
rollout.
