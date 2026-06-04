# etcd access hardening

## Status
**Phase 1 implemented and validated on the dev container cluster** (see
`docs/design/virtual-test-environment.md`). Phase 2 (close the boundary) not
yet started.

Phase 1 changes:
- admin-api no longer holds an etcd client on non-storage nodes. Every
  etcd-touching path in the health surface is gated behind `is_etcd_node()`
  (`get_current_node_type() == 'storage'`, i.e. `s*`): the startup
  "wait for etcd" loop, the `/test` connectivity probe, `is_storage_leader`,
  `is_dhcp_leader`, `is_node_drained`, `check_certificate_expiry`, and the
  keepalived check's `get_core_nodes()` (which did a full `by-hostname/`
  prefix scan just to ask "am I core?"). Verified: a compute node starts
  without etcd, serves `/metrics` + `/api/health` with the etcd-dependent
  services reported `not_applicable`/`false`, and holds **zero** TCP
  connections to `:2379` (confirmed by stack-trace instrumentation and live
  socket inspection).
- `ycluster inventory collect` gained a `--print` mode (collect locally, emit
  JSON, no etcd write). `collect-hardware-facts.yml` now mirrors the macOS
  flow: each node collects locally and the etcd write is delegated to a
  storage node. Verified: a compute node's hardware record lands in etcd
  (written via the storage node) while the compute node itself makes no etcd
  connection.

A standing principle came out of this: avoid unnecessary etcd calls in
general. The core-node health path still makes ~6 reads per scrape — tracked
as a perf follow-up in `TODO.md`.

## Problem
etcd is the single source of truth for cluster state and secrets, yet it
has **no authentication and no transport security**, and **no firewall**
restricts who may connect.

From `install-etcd.yml`, every config variant sets:

```
ETCD_LISTEN_CLIENT_URLS="http://0.0.0.0:2379"
```

Plaintext HTTP, bound to all interfaces. There is no etcd user/role/RBAC
setup and no `--client-cert-auth` anywhere in the repo. The only iptables
rules in the tree (`setup-gateway-vip.yml`) are NAT/forwarding for the
`10.0.0.0/24` cluster network — there are **no `INPUT`-chain rules** on
the core nodes filtering inbound connections to 2379/2380.

Consequence: any host that can open a TCP connection to a core node's
2379 has full unauthenticated read/write over the entire keyspace,
including:

- `/cluster/config/inference/master-key` (the inference admin bearer)
- the Open-WebUI secret key (`<owui-prefix>/secret-key`)
- all node registration, DHCP leases, leader-election, and service state

The cluster is a flat `/24` with L2 reachability between all node types
(storage `s*`, compute `c*`, nvidia `nv*`, nas `nas*`, macOS `m*`,
adhoc `x*`). So the protection boundary today is "be on the cluster
subnet," not "be a core node."

## Why a source-IP firewall is not sufficient on its own
Blind off-LAN spoofing of an etcd connection is hard — etcd is gRPC over
HTTP/2 over TCP, so an attacker must complete a bidirectional handshake,
and Linux ISN randomization defeats off-path spoofing.

But the threat model here is an attacker **on the same L2 segment** (the
untrusted compute/adhoc nodes and the ephemeral GPU-passthrough VMs).
On a shared segment, IP-based identity is forgeable: ARP poisoning lets
an attacker redirect return traffic and complete a TCP session while
impersonating a core node's IP, unless the switch enforces Dynamic ARP
Inspection / port security (a small cluster generally does not). So a
source-IP firewall is a network-*position* boundary, and position is
forgeable by exactly the nodes we want to exclude.

A real fix needs a **cryptographic identity** boundary (mTLS), OR the
non-core nodes must stop touching etcd so the position boundary only has
to hold for nodes we already trust.

## WireGuard does not help here
The existing WireGuard setup (`setup-wg.yml`, `wg_config.py`) is a
**remote-access overlay**, not an internal node-to-node mesh: the server
runs on the gateway-VIP holder, peers are external clients on a separate
`10.0.1.0/24`, and the server advertises `AllowedIPs = 10.0.0.0/24`
(i.e. it *grants peers reach into* the cluster LAN). Cluster nodes talk
to etcd over plain `10.0.0.0/24`. Repurposing WG to gate etcd would mean
building a new internal mesh among core nodes + every etcd client —
comparable effort to mTLS or more.

## Who actually talks to etcd (audit)

etcd runs on `core` (s1–s3). Clients:

| Consumer | Node types | Direct etcd? | Essential? |
|---|---|---|---|
| `dhcp-leader-election`, `storage-leader-election` | core only | yes | yes |
| certbot-manager, `tls_config.py`, `collect-model-stats` | core only | yes | yes |
| `populate_local_node` (`ycluster cluster populate-local-node`) | core (storage) | yes | yes |
| `admin-api.service` | **all `managed`** (storage + compute + nvidia + adhoc + nas) | yes (`ETCD_HOSTS=<core>` in unit) | **mostly cruft on non-core** |
| `ycluster vm` CLI (`vm_manager.py`) | **compute (incus hosts)** | yes (read/write/delete) | **yes — real** |
| `ycluster inventory collect` | storage:compute:nas:adhoc | yes (writes hardware facts) | **cruft — delegatable** |
| hardware-facts collection (macOS) | macOS | **no** — runs script locally, writes from a storage node via `delegate_to` | n/a (the precedent) |
| frontend `f*` | — | no (not in `managed`) | n/a |

`managed = storage + compute (+nvidia) + adhoc + nas`
(`inventory_plugins/etcd_nodes.py:96-100`). `install-incus.yml:294-301`
literally writes `ETCD_HOSTS=10.0.0.11:2379,...` to `/etc/environment`
with the comment *"non-core nodes have no local etcd, so point the etcd
client at the core nodes."*

### The non-core access is mostly cruft

**1. `admin-api.service` on non-core nodes — mostly cruft.**
Deployed cluster-wide (`setup-web-services.yml` is `hosts: managed`) with
`ETCD_HOSTS=<core>:2379` baked into the unit (`setup-web-services.yml:66`).
On a non-core node its only always-on etcd use is the health/metrics path
(`/metrics`, scraped per-node by Prometheus → `get_comprehensive_health`):

- `client.get('/test')` — an "is etcd reachable?" connectivity probe
- `is_storage_leader()` → reads `/cluster/leader/app`
- `is_dhcp_leader()` → reads `/cluster/leader/dhcp`

Both leader reads are trivially `False` off-core (a compute/nas node can
never hold those leases), so the access exists almost entirely to report
telemetry that is meaningless off-core. The heavy routes (`/api/allocate`,
`/api/wg/register`, `/api/dhcp-config`, `/bootstrap/*`, `/autoinstall/*`,
drain, inventory) are reached by clients via the storage VIP
(`admin.xc` → 10.0.0.100, core-only) and PXE — served by every instance
but only exercised on core. *(One thing inferred, not proven: that the
heavy routes are only ever hit via the VIP, never per-node. Confirm by
tracing how PXE/DHCP and WG-bootstrap address admin-api before removing
anything.)*

**2. `ycluster inventory collect` — cruft (redundant pattern).**
Each Linux non-core node calls `put_hardware()` → direct etcd write. But
macOS already proves this is unnecessary: `collect-hardware-facts-macos.yml`
runs the collection script on the node, copies the JSON to a storage
node, and writes to etcd from there via `delegate_to`. Linux non-core
nodes could follow the same pattern and drop direct etcd access.

**3. `ycluster vm` CLI on compute (incus) — genuinely needed.**
`vm_manager.py` does live read/write/delete against etcd to allocate IPs,
track VM records, and manage SSH keys for the ephemeral GPU-passthrough
VMs. This is the one substantive non-core dependency. It is an operator
CLI, not an always-on service.

## Proposed direction

The audit collapses the problem: of the non-core etcd access, two of the
three consumers are removable, leaving only the VM CLI on compute hosts.

**Phase 1 — remove the cruft (do this regardless of the final boundary):**

1. Gate admin-api's etcd/leadership health checks behind `is_storage_node`
   (or stop shipping the etcd-configured unit to non-core), so non-core
   admin-api no longer holds an etcd client. Loss: a per-node "etcd
   reachable?" metric, which is undesirable once etcd is locked down.
2. Make Linux `inventory collect` delegate its etcd write to a storage
   node, mirroring the macOS flow. Removes the compute/nas/adhoc write
   path.

After Phase 1 the only non-core etcd client is `ycluster vm` on compute.

**Phase 2 — close the boundary (pick one):**

- **(a) Proxy VM allocation through the authenticated admin API.** Replace
  `vm_manager.py`'s direct etcd calls with calls to an admin-api endpoint
  on the storage VIP. Then *no* non-core node touches etcd, and etcd can
  be firewalled to `s*` only — the position boundary now only has to hold
  among already-trusted core nodes. Simplest end state; most app work.
- **(b) mTLS on etcd.** `--client-cert-auth` + a trusted CA; every etcd
  client gets a cert. Identity is cryptographic, so on-LAN ARP/IP games
  don't help an attacker. After Phase 1 the only non-core client needing
  a cert is the compute VM hosts. The repo already has CA/issuance tooling
  (`ycluster tls generate`, `tls_config.py`, `fetch_tls_certs.py`); the
  work is threading certs into the etcd client call sites
  (`common/etcd_utils.py`, the leader-election shell scripts'
  `etcdctl`, certbot-manager) and flipping the etcd flags.

Recommendation: do Phase 1 now (pure cleanup, reduces attack surface and
removes the worst-trust nodes from etcd). Then prefer (a) if VM
allocation is comfortable behind the admin API, since it yields the
simplest, cert-free locked-down end state; fall back to (b) if direct
etcd access from compute must stay.

## Out of scope / open questions
- Confirm admin-api heavy routes are VIP-only (see note above).
- etcd RBAC (per-client key-range authz) is *not* needed for the
  all-or-nothing goal; mTLS alone authenticates. RBAC only matters if we
  later want differentiated permissions per client.
- Whether to also move etcd off plaintext for the peer port (2380) and
  enable server TLS for confidentiality even within core.
