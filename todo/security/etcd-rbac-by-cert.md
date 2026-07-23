# etcd RBAC by client cert

Phase 2 of `../../docs/design/etcd-access-hardening.md` shipped mTLS but
kept **one** shared client identity (`CN=etcd-client`) for every holder of
the cert (`setup-etcd-tls.yml:209` → `hosts: storage:compute:nas`). Every
holder has full read/write on the entire keyspace. The trust boundary is
"you have a client cert" — there is no per-subtree scoping, so a
compromise of an Incus host (which runs `ycluster vm` and holds the same
cert) leaks every secret in etcd (master-key, db-password, TLS private
key, WG server privkey, rathole/frp tokens — full inventory below).

Split the cert identity and turn on etcd RBAC.

## Identity split

Two roles. The previous draft distinguished core (s1–s3) from storage
(s4+) but the distinction is moot in practice: s4+ is storage-leader
eligible (`../operations/s4-leader-validation.md`), so anything a
storage leader needs (master-key, db-password, TLS private, WG server,
rathole token) must be readable by every `s*`. And anything an
attacker on a compromised `s*` can already do via read access is
effectively game-over (they can impersonate the leader, mint tokens,
etc.) — gating writes behind a separate cert doesn't recover from a
read-capable compromise. So collapse them.

- **etcd-storage** — every `s*` (s1–s3 + s4+). Full keyspace (`rw`).
  The s1–s3 nodes additionally hold the etcd server/peer cert (Phase
  2); they use the same `etcd-storage` client cert as s4+.
- **etcd-compute** — c*/nv*/nas*. Limited to `/cluster/users/`,
  `/cluster/vms/`, `/cluster/vms-events/`, `/cluster/vm-state/`,
  `/cluster/vm-desired/`, `/cluster/vm-grace/`, `/cluster/vm-issue/`.
  Read/write on the VM subtree; **no read on any secret** — this is
  the boundary that matters.
- (No cert for m*, f*, x* — unchanged.)

`/etc/ycluster/etcd-client.env` becomes per-service (`etcd-storage.env`,
`etcd-compute.env`) consumed by the units/scripts that need that
identity.

## Secrets inventory (what the `/cluster/config/` scope protects)

Inventory of every etcd key that holds (or sits next to) a secret or
credential. The split above grants **no read on `/cluster/config/`**
to compute; storage (all `s*`) reads them. Items marked **(secret)**
contain material that must never reach a compromised compute/Incus
host; items marked **(credential-adjacent)** are public-ish but gate
access and so still need policy.

### `/cluster/config/` — sensitive

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/config/inference/master-key` | **(secret)** cluster admin bearer token | Bypasses per-user auth at `local-ai-proxy-auth`; valid for any model. Leaked key = full inference access as admin. |
| `/cluster/config/usage_stats/db-password` | **(secret)** postgres password | Grants DB access to usage_stats; rotates via `admin-stats.yml` (no formal rotation flow — `secrets-rotation.md`). |
| `/cluster/config/authentik/bootstrap-token` | **(secret)** authentik bootstrap API token | Admin-level API access to authentik for the lifecycle that creates users/properties. |
| `/cluster/config/inference/models/<name>` | model→backend map (`{"backends":[{"api_base":"…"}, …]}`) | Not strictly secret, but leaks the model inventory and backend URLs. |
| `/cluster/config/inference/disabled/<url>` | operational state | Not secret. |

### `/cluster/tls/` — sensitive

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/tls/key` | **(secret)** PEM private key for cluster TLS | The cluster's external HTTPS private key. Leaked = impersonate the cluster to any client trusting the cluster CA. Written by `certbot_manager.py:438`; read by `fetch_tls_certs.py`, `certbot.py`. |
| `/cluster/tls/cert` | PEM fullchain | Public. |
| `/cluster/tls/common_name` | primary CN string | Public. |

### `/cluster/wg/` — sensitive

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/wg/server` | **(secret)** `{privkey, pubkey, port, endpoints, server_ip}` | WireGuard server private key. Leaked = full tunnel into the cluster from anywhere. Written by `wg_config.py` (operator action). |
| `/cluster/wg/peers/<hostname>` | `{pubkey, pubkey_sha256, status, created_at, approved_at}` | Public keys; not secret. |

### `/cluster/nodes/{rathole,frp}/config` — sensitive

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/nodes/rathole/config` | **(secret)** `{remote_addr, token}` | Shared secret for tunnel auth at every rathole client. Leaked = anyone with the URL can open a tunnel as if they were the bastion. |
| `/cluster/nodes/frp/config` | **(secret)** `{remote_addr, token}` | Same shape, frp migration target (`networking/switch-rathole-to-frp.md`). |

### `/cluster/users/` and `/cluster/backup/` — credential-adjacent

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/users/<email>` | **(credential-adjacent)** `{ssh_keys: [...]}` | Public SSH keys (never private). Possession of an authorized key is what gates VM SSH via the bastion; revoking requires removing the entry. |
| `/cluster/backup/recipients/<name>` | age public key | Public; private age key is held out-of-band per `docs/operations/disaster-recovery.md`. |

### `/cluster/https/` and `/cluster/healthchecks/` — low sensitivity

| Key | Holds | Why it matters |
|---|---|---|
| `/cluster/https/domain`, `/cluster/https/aliases` | domain config | Public; identifies the cluster externally. |
| `/cluster/https/email` | certbot contact email | PII-adjacent; used for Let's Encrypt expiry notices. |
| `/cluster/https/certificate_info` | `{primary_domain, domains, cert_path, key_path, …, updated_at}` | Metadata only; `key_path` references a private key on disk but does not contain it. |
| `/cluster/healthchecks/url` | healthchecks.io ping URL | Operational; leak enables spoofed pings. |

### Everything else — operational state, not secret

`/cluster/nodes/by-hostname/`, `/cluster/nodes/by-mac/`, `/cluster/nodes/<h>/drain`,
`/cluster/nodes/frontend/`, `/cluster/services/` (leader election),
`/cluster/dhcp/leases/`, `/cluster/leader/app`, `/cluster/vms/`,
`/cluster/vms-events/`, `/cluster/vm-state/`, `/cluster/vm-desired/`,
`/cluster/vm-grace/`, `/cluster/vm-issue/`, `/cluster/backup/destinations/`.
Leaks of these would be information disclosure at worst (host
inventory, leader state) — covered by the storage-tier read scope.

## Open trade-offs

- **`/cluster/tls/key` is read and written by certbot on the storage
  leader.** With the merged `etcd-storage` role, that's a non-issue —
  certbot can both read and renew the key. We lose the (already
  theoretical) ability to deny s4+ the ability to mint a TLS key,
  but a compromised s4+ that could read it could already mint one
  by other means (it holds the cluster CA cert in
  `/etc/ycluster/ca/`).
- **Rathole/frp key split.** `gateway_health.py` on storage-VIP
  holders parses `/cluster/nodes/rathole/config` for **the port only**
  (`parse_rathole_port`) — it never uses the token. Cleaner shape:
  split into `/cluster/config/tunnel/{rathole,frp}/port` and
  `/cluster/config/tunnel/{rathole,frp}/token`, written together by
  `set_rathole_config`/`set_frp_config`. Pairs with the rathole→frp
  migration in `../networking/switch-rathole-to-frp.md` — do it during
  that cutover.
- **What the merge loses.** The old three-role draft could have
  written some secrets (`master-key`, `wg/server`) as core-only and
  made s4+ read-only on them — a "compromise of s4+ can leak but not
  forge" boundary. That boundary was always thin (a read-capable s4+
  is game-over anyway), and we don't have an operational need for it
  today. If one appears later, the fix is a third cert + role, not a
  rewrite of this split.

## Prefix consolidation (proposal)

Today, true secrets live under four different prefixes — `/cluster/config/`,
`/cluster/tls/`, `/cluster/wg/`, `/cluster/nodes/{rathole,frp}/config` —
which is why the RBAC grant list above needs five separate `read`
permissions. Move every secret-bearing subtree under `/cluster/config/`
so a single `read /cluster/config/` grant covers them all.

Target tree (only the keys that hold secrets or sit next to them are
moved; operational state stays where it is):

```
/cluster/config/
  inference/
    master-key                  (was /cluster/config/inference/master-key — same)
    models/<name>               (kept for grouping; not secret)
    disabled/<url>              (kept; not secret)
  usage_stats/db-password       (was /cluster/config/usage_stats/db-password — same)
  authentik/bootstrap-token     (was /cluster/config/authentik/bootstrap-token — same)
  tls/                          (moved from /cluster/tls/)
    key                         (the secret)
    cert                        (public; moves only for grouping)
    common_name                 (public)
  wg/                           (moved from /cluster/wg/)
    server                      (the secret)
    peers/<hostname>            (public; moves only for grouping)
  tunnel/                       (moved from /cluster/nodes/{rathole,frp}/config)
    rathole/port, rathole/token (post-split)
    frp/port, frp/token         (post-split)

# unchanged — operational state
/cluster/nodes/by-hostname/, by-mac/, drain, frontend/, hardware/, asset/
/cluster/services/
/cluster/dhcp/leases/
/cluster/leader/{app,dhcp}
/cluster/users/<u>              (public SSH keys — credential-adjacent, not secret)
/cluster/vms/, vms-events/, vm-state/, vm-desired/, vm-grace/, vm-issue/
/cluster/backup/destinations/, recipients/  (recipient pubkeys — public)
/cluster/https/{domain,aliases,email,certificate_info}
/cluster/healthchecks/url
```

After consolidation the RBAC grants collapse to two roles:

```
# Storage = all s* (s1-s3 + s4+). Full keyspace.
etcdctl role add storage && etcdctl role grant-permission storage readwrite "" --prefix

# Compute = c*/nv*/nas*. VM subtree only, no read on secrets.
etcdctl role add compute && etcdctl role grant-permission compute readwrite /cluster/users/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vms/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vms-events/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vm-state/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vm-desired/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vm-grace/ --prefix
                                       etcdctl role grant-permission compute readwrite /cluster/vm-issue/ --prefix

etcdctl user grant-role etcd-storage storage
etcdctl user grant-role etcd-compute compute
```

That's the entire RBAC surface: **one `rw` grant per role**. The
prefix consolidation removes every other line.

The benefits are:

- **One role, one grant per role.** Two cert identities, two RBAC
  roles, two `--prefix` lines. Adding a future secret doesn't require
  editing the RBAC playbook — it lands under `/cluster/config/` and
  picks up storage `rw` automatically.
- **One prefix to audit.** `etcdctl get --prefix /cluster/config/`
  enumerates every secret the cluster holds — currently requires four
  prefixes plus the `/cluster/nodes/rathole/config` blob.
- **Conceptual split is obvious.** `/cluster/config/` = "configuration
  + secrets", the rest = "operational state". Aligns with the
  disaster-recovery narrative in `docs/operations/disaster-recovery.md`,
  which already segregates these by `/cluster/config/authentik/` vs
  the operational prefixes.
- **The rathole/frp split lands cleanly.** Splitting the existing blob
  into `/cluster/config/tunnel/{rathole,frp}/{port,token}` happens in
  the same migration; one rename pass replaces four touchpoints.

The cost is a structural rename across:

- Writers: `certbot_manager.py`, `wg_config.py`, `rathole_config.py`,
  `frp_config.py`, `admin-stats.yml`, `authentik_manager.py`,
  `setup-etcd-tls.yml`, `healthchecks.py`, the TLS/rathole/frp install
  playbooks.
- Readers: `fetch_tls_certs.py`, `certbot.py`, `gateway_health.py`,
  `app.py` (`check_certificate_expiry`, the schema header at the top
  of the file), `local-ai-proxy-auth.py`, `monitoring/files/update-blackbox-targets.py`,
  `health-api.py`.
- Tests: `tests/test_health_api.py`, `tests/fake_etcd.py`, the
  `test_*` fixtures that prime `/cluster/tls/`, `/cluster/wg/server`,
  `/cluster/nodes/rathole/config`.
- Docs: `docs/operations/etcd.md`, `docs/operations/disaster-recovery.md`,
  `docs/operations/inference.md`, `docs/design/frp-migration.md`, the
  schema comment in `app.py`.

### Migration

One-shot rename, piggybacked on the cert-identity rollout so it lands
in the same `enforce` step. `migrate-etcd-prefixes.yml`:

1. For every key in the rename table, `txn` over etcd: read old → put
   new (same value) → delete old. Atomic per key.
2. Run before flipping the cert to the new identity so the read of the
   old key still has permission.
3. Idempotent: re-running on a fully-migrated cluster is a no-op
   (the read of the old key returns empty).
4. After the rename, the RBAC grants listed in the matrix above are
   applied to the **new** prefixes; old-prefix grants are removed.

### Backwards compat during cutover

Don't dual-read — the cert split is the gating event, and the rename
runs in the same `enforce` window as the cert swap. Any node still on
the old cert can't read the new key, so a half-migrated cluster
fails closed (RBAC denies), not open (stale data). Add a dry-run
mode to `migrate-etcd-prefixes.yml` that prints the rename plan
without touching etcd, so an operator can review before the cutover.

### Applying RBAC after consolidation

After the rename + cert swap, ship a `setup-etcd-rbac.yml` that diffs
the live roles/grants against the desired set (treat as a no-op when
equal), so the procedure is `ansible-playbook` not hand-typed
`etcdctl`. The desired set is exactly the collapsed grants shown
above — two roles, two `--prefix` lines (storage `rw ""`, compute
`rw` on the VM subtree) — and the diff catches any drift introduced
by hand.

## Migration

Same ladder pattern as Phase 2 (`off`/`listen`/`connect`/`enforce`),
applied to **cert identity**: a single `etcd_rbac_phase` var rolls the
new identities out and flips the role grants on one node at a time. Old
shared cert stays valid until `enforce` so non-migrated nodes keep
working; admin-api on non-storage is already etcd-free (Phase 1), so it
is unaffected by the split.

## Validation

After enforce, on an `nv*` (compute) host with the new `etcd-compute`
cert:

- `etcdctl get /cluster/config/inference/master-key` → "permission
  denied"
- `etcdctl put /cluster/vms/foo '{"owner":"x"}'` → OK
- `etcdctl put /cluster/config/foo bar` → "permission denied"
- `ss -tnp | grep 2379` shows connections from this host (still uses
  etcd for its VM subtree), proving the cert is live but the policy is
  enforced.

On `s1` with `etcd-storage`: same `master-key` read still works.

## Paired items

- `secrets-rotation.md` — once secrets are reachable only by storage
  (not compute), rotation flow can stop treating non-storage holders
  as a leak surface.
- `core-group-definition.md` — superseded by this design (no
  distinction between s1–s3 and s4+ anymore); the existing
  ambiguity can be resolved by picking one definition (the broader
  "all s*" matches the new RBAC role).
