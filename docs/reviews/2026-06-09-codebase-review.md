# Codebase Review — 2026-06-09

Full-repo review covering security, correctness, Ansible hygiene, and architecture.
Method: five parallel review passes (security surface, Go proxy, Ansible, Python
services, architecture), followed by hand-verification of every critical finding
against the source. One false positive was discarded during verification: the
claim that RBD failover lacks exclusive locking is wrong — `user-rbd-manager:206`
uses `rbd map --exclusive`; only the setup-time playbooks omit it (see A3).

**Summary:** The architecture is sound — etcd as single source of truth, phased
mTLS rollout, exclusive-locked RBD failover, Clevis/Tang encryption, and vault
discipline are all done well. The real exposure is concentrated in the **admin
API** (unauthenticated mutating endpoints, Flask dev server running as root) and
the **bootstrap trust model** (anyone on the provisioning LAN can become a
cluster node). Plus a handful of verified small bugs with one-line fixes.

Line numbers reference the tree as of commit `f1e3305`.

---

## 1. Verified bugs — quick wins

- [ ] **B1. Timing-unsafe master-key comparison**
  `config/ansible/app/files/local-ai-proxy-auth.py:117` uses `token == master`.
  Fix: `secrets.compare_digest(token, master)`. Practical exploitability over a
  network is low, but the fix is free.

- [ ] **B2. Allocation transaction result ignored**
  `config/ansible/admin/files/app.py:326-336`: the etcd transaction has
  `failure=[]` and its return value is never checked; `allocation_data` is
  returned as if committed. The in-process `allocation_lock` does not protect
  against the DHCP server (which also allocates) or a second admin-api
  instance — on compare failure the caller gets a hostname/IP that was never
  written or collides with another node. Fix: check the transaction result,
  retry or raise on failure.

- [ ] **B3. DHCP allocation check-then-use race**
  `config/ansible/admin/files/ycluster/ycluster/utils/dhcp_server.py` (~:473-545):
  hostname existence check is non-atomic with the write; transaction uses empty
  `compare=[]`. Same class of bug as B2 — include `version(...) == 0` compares
  in the transaction.

- [ ] **B4. Go proxy retries every ≥400 status on the next backend**
  `local-ai-proxy/handler.go:324`. A 400/401/404 (bad request, model missing on
  one backend, auth misconfig) is replayed against every backend — duplicates
  non-idempotent POST work and masks real client errors as "all backends
  failed". Fix: retry only 5xx/429 (404 too only if model-placement drift
  tolerance is wanted, as a deliberate choice).

- [ ] **B5. SQL string interpolation**
  `config/ansible/admin/files/provision-usage-stats.py:35`:
  `f"CREATE USER ... PASSWORD '{password}'"`. Password comes from etcd so risk
  is low, but a `'` in it breaks the statement. Use a parameterized statement
  or `quote_literal`.

- [ ] **B6. `wipe-etcd.yml:4` has `become: core`**
  Not a valid `become` value (boolean expected) — playbook is broken or
  silently not escalating. Should be `become: true`.

- [ ] **B7. Unverified binary downloads**
  Qdrant tarball (`config/ansible/storage/install-qdrant.yml`) and the Ubuntu
  ISO (`config/ansible/admin/setup-pxe-boot.yml`) fetched with no checksum.
  The ISO is the root of trust for every PXE-provisioned node. Add
  `checksum: sha256:...` to the `get_url` tasks.

- [ ] **B8. Bare `except:` in the etcd inventory plugin**
  `config/ansible/inventory_plugins/etcd_nodes.py:132` swallows all connection
  errors; if every etcd host fails, Ansible gets an empty inventory with no
  diagnostic and "succeeds" against nothing. Catch specific exceptions and warn
  per failed host.

## 2. Admin API hardening (highest security ROI)

`config/ansible/admin/files/app.py` (~2700 lines) accumulates issues that
compound, and it is the DHCP/PXE/bootstrap brain of the cluster:

- [ ] **S1. Flask dev server, as root, on 0.0.0.0**
  `app.py:2721` (`app.run(host='0.0.0.0', port=12723)`) plus
  `config/ansible/admin/setup-web-services.yml:67`
  (`ExecStart=/usr/bin/python3 app.py`, `User=root`, no systemd hardening).
  Fix: gunicorn/waitress, dedicated user, `NoNewPrivileges` /
  `ProtectSystem=strict` in the unit.

- [ ] **S2. Mutating endpoints have no authentication**
  `/api/host/<hostname>/disable|enable` (app.py:506,530), drain endpoints,
  `/api/allocate`. Anything on 10.0.0.0/24 (including a compromised compute or
  adhoc x-node) can disable hosts or claim allocations. Extends the existing
  TODO item about drain auth to all mutating endpoints; the master key in etcd
  is available to validate against.

- [ ] **S3. Route params flow unvalidated into etcd keys**
  `<hostname>` from the URL is interpolated into
  `f"{ETCD_PREFIX}/by-hostname/{hostname}"` — a crafted hostname can address
  adjacent etcd namespaces. Fix: validate with `^[a-z]+[0-9]+$` (or per-type
  regex) before use.

## 3. Architectural risks (not already tracked in TODO.md)

- [ ] **A1. Bootstrap is trust-on-first-use keyed to MAC addresses.**
  A rogue device on the provisioning VLAN can spoof a storage-range MAC, PXE
  boot, receive autoinstall user-data (password hash, SSH key), and join the
  cluster. `/bootstrap/*` scripts are served unsigned and piped to `sudo bash`.
  A LAN trust model is defensible for this cluster, but: (a) state it
  explicitly in ARCHITECTURE.md; (b) cheap hardening — validate MAC OUIs,
  serve bootstrap scripts with a checksum the wrapper verifies.

- [ ] **A2. API keys stored in plaintext.**
  Master key in etcd and Open-WebUI `api_key` rows are plaintext — a DB or
  etcd read compromise yields working credentials. Hashing Open-WebUI keys
  needs upstream changes; may be accept-and-document, but belongs in the
  threat model.

- [ ] **A3. Setup-time RBD maps lack `--exclusive`.**
  Runtime mounts use `rbd map --exclusive` (`user-rbd-manager:206`, good), but
  `config/ansible/storage/setup-user-rbd.yml:68` and
  `config/ansible/storage/setup-misc-rbd.yml:29` don't — running setup while
  the election manager holds the volume could fight it. Add `--exclusive`
  for consistency.

- [ ] **A4. Go proxy etcd-watch resilience.**
  `local-ai-proxy/source.go:134`: a broken watch (compaction, channel close)
  isn't detected/restarted — model config silently stops hot-reloading until
  service restart. Similarly the disabled-backends set goes stale if etcd is
  briefly unavailable (`disabled.go:32`). Both want a watch-error →
  re-list-and-rewatch loop.

## 4. Ansible hygiene (lower priority, batchable)

- [ ] **H1.** `groups['storage'][0]` delegation appears ~20× in
  `config/ansible/storage/setup-user-rbd.yml` despite the project's
  mountpoint-based leader-detection convention — breaks under `--limit`.
- [ ] **H2.** `ignore_errors: yes` on stop/teardown paths (`wipe-etcd.yml`,
  `storage/stop-storage-leader-election.yml`) can hide a hung service before
  destructive next steps.
- [ ] **H3.** systemd units for the DHCP server
  (`admin/install-dhcp-leader-election.yml`) and admin API have no hardening
  directives; even where root is required for raw sockets,
  `NoNewPrivileges`/`ProtectHome`/`ProtectKernelModules` are free.
- [ ] **H4.** `admin/install-vm-bastion.yml` reads the rathole token via
  etcdctl without `no_log` on the registering task — a failure can splash the
  token into Ansible output.

## 5. Lower-severity / noted

- Go proxy: health `Probe()` goroutines use bare `context.Background()` with
  no timeout (`health.go:142`); large 4xx bodies only partially drained on
  retry (`handler.go:329`); ACL is allow-by-default for unlisted models
  (`acl.go` — by design, but worth a "deny unknown" mode flag).
- Python: global etcd client cached forever (`etcd_utils.py:86`) — stale after
  member changes; mixed `print(file=sys.stderr)` vs logging in app.py; MAC
  normalization duplicated across ~5 files; nginx `-t` stderr discarded in
  `certbot_manager.py:215`.
- `/api/allocations`, `/api/health` expose full topology/health detail
  unauthenticated on the cluster network (recon value only; fine under the
  LAN trust model once stated).

## 6. Already acknowledged in TODO.md (not re-litigated here)

etcd quorum-loss DR runbook, gateway VIP health-check brittleness, s4 VIP
priority bug, Ceph/RBD backup story, WG ingress failover, rathole PROXY
protocol (per-client rate limiting), drain-button auth, monitoring gaps,
Incus storage migration, node lifecycle runbooks, secrets/SSH-key rotation,
audit logging.

## 7. What's done well

Phased etcd mTLS rollout (off→listen→connect→enforce, with migration test
harness in `dev/`); `rbd map --exclusive` fencing with aggressive teardown on
leadership loss; Clevis/Tang 2-of-3 LUKS unlock; vault-symlink secret
separation; atomic rathole binary replacement (ETXTBSY-safe); nginx
`internal;` on the auth_request location; parameterized SQL in the auth
service's api_key lookup; Go proxy context propagation, graceful shutdown,
and test coverage; secrets properly gitignored.

## 8. Suggested order of attack

1. Section 1 (B1–B8): mechanical fixes, roughly an afternoon total.
2. Section 2 (S1–S3): admin API — gunicorn + non-root + validation + auth.
3. A3 + B8 follow-ons; A4 watch resilience.
4. A1: document the LAN/TOFU trust model; bootstrap integrity checking when
   convenient.
5. TODO.md items as already triaged — etcd DR runbook first (only
   unrecoverable failure mode).
