# Changelog

Completed work, moved out of `TODO.md`. Newest first. Items that still have
residual or follow-on work remain in `TODO.md` until fully closed.

## 2026-06-10

### Security & correctness

- **Verified bugs B1–B8** (from `docs/reviews/2026-06-09-codebase-review.md`) — all fixed and deployed; canary-validated on s2, B2 also live-tested on the dev cluster.
  - B1 — timing-unsafe master-key compare (`local-ai-proxy-auth.py:117`): switched `token == master` to a timing-safe `secrets.compare_digest` on bytes.
  - B2 — allocation etcd transaction result ignored (`admin/files/app.py:326`): CAS result now checked with retry/re-read so a compare failure can't return an uncommitted/colliding hostname-IP.
  - B3 — DHCP allocation check-then-use race (`utils/dhcp_server.py` ~:473–545): added `version(...)==0` compares, in-transaction old-hostname delete, and retry.
  - B4 — Go proxy retried every ≥400 on the next backend (`handler.go:324`): retries only 5xx/429/404 now (404 kept deliberately for model-placement drift), so non-idempotent POSTs aren't replayed and client errors aren't masked.
  - B5 — SQL string interpolation (`admin/files/provision-usage-stats.py:35`): parameterized; psql via stdin + `ON_ERROR_STOP` + escaped literal.
  - B6 — `wipe-etcd.yml:4` had invalid `become: core`; corrected to `become: true`.
  - B7 — unverified downloads: added `checksum:` to the Qdrant tarball (`storage/install-qdrant.yml`, sha256 TOFU, binary matched deployed) and the Ubuntu ISO (`admin/setup-pxe-boot.yml`).
  - B8 — bare `except:` in `inventory_plugins/etcd_nodes.py:132` yielded a silent empty inventory; now catches specific exceptions, warns per host, and resets broken clients.

- **Admin API hardening (S1) — privilege drop.** admin-api now runs as a dedicated `admin-api` system user under waitress (single process, 8 threads to preserve `allocation_lock` semantics) with `NoNewPrivileges`/`ProtectSystem=strict`/`ProtectHome`/`PrivateTmp`, fully sudo-free. Group grants only: `shadow` (autoinstall password hash) and `etcd-client` (client TLS key). The ceph health check uses a dedicated read-only ceph identity (`client.admin-api`, `mon allow r`, keyring root:admin-api 640, provisioned by `setup-web-services.yml`); the docker check reports systemd unit state only; the secrets_mount check treats mounted-but-unreadable as healthy.

- **Admin API hardening (S2) — mutating endpoints removed, mutations moved to mTLS CLI.** Previously anything on 10.0.0.0/24 could disable hosts or claim allocations via unauthenticated POSTs. Dev-cluster validated, canaried on s2, rolled out fleet-wide (incl. blackbox cert reissue; all Prometheus probes green).
  1. CA merge: `ca_manager.py` ported to `/etc/ycluster/ca` (the unified etcd CA), gated on CA-key presence instead of storage leader, refuses to clobber an existing CA, skips reissuing certs valid >30d; `install-blackbox.yml` mints via a `run_once` play on the etcd group.
  2. Mutating endpoints removed from `app.py`: `/api/host/<h>/disable|enable`, `/api/drain[/h]`, `/api/undrain[/h]`, `PUT /api/inventory/asset/<h>`. Reads unchanged.
  3. CLI-only mutations: `ycluster cluster drain|undrain|disable|enable` write etcd directly via the new `ycluster/utils/host_state.py` (with allocation-existence check, so a typo'd hostname errors instead of writing a stray key); `ycluster inventory set-asset` already wrote etcd directly.
  4. UI: status-page drain column and inventory page are now read-only (the latter points at `set-asset`).
  5. Remote *reads* stay on 0.0.0.0:12723 deliberately (central Prometheus scrapes, leader `/api/health` polling, macOS health-service share the port convention). Remaining open POSTs are the TOFU bootstrap surface (`/api/allocate`, `/api/wg/register`) and `/api/alert-webhook`.

- **Admin API hardening (S3) — route param validation.** `@validated_hostname` decorator (`^[a-z]{1,4}[0-9]{1,3}$|dhcp-NNN`) on all nine `<hostname>`/`<target_hostname>` routes → 400 before any etcd interpolation; `<node_type>`/`<job>` params already dict-validated.

- **Drain-function authentication — superseded.** The status-page mutation UI (drain/disable buttons) is deprecated; mutations are now CLI-only behind client-cert mTLS (see S2 above), so the original "authenticate the drain function" item no longer applies.

- **H4 — `no_log` audit across the Ansible tree.** Added `no_log: true` to every task handling a secret in cleartext: the bastion rathole-config read (`admin/install-vm-bastion.yml`), the MicroCeph join-token generate/extract/join trio (`storage/add-ceph-nodes.yml`), the TOTP-seed slurp + install (`admin/setup-admin-user-tasks.yml`), and the admin-api ceph keyring create + install (`admin/setup-web-services.yml`). Also removed the rathole token from the inventory plugin; `app/install-rathole-server.yml` now reads it from etcd at task time with `no_log`, so `ansible-inventory --host <frontend>` no longer prints it.

- **etcd access hardening — mTLS-only.** All core nodes run etcd in `enforce` mode with no plaintext listeners; cert-possession is the access boundary (see `docs/design/etcd-access-hardening.md`). CA unification (`ca_manager.py` → `/etc/ycluster/ca`, blackbox certs reissued from the unified CA) landed as S2 step 1 above. The dead RBD CA tree was renamed to `/rbd/misc/ca.old` post-rollout and deleted 2026-06-13 after burn-in.

- **Reduced vault-secret exposure in `ansible-inventory`** (commit `297d1c0`). `ansible-inventory --host <anyhost>` previously printed vaulted group_vars (`vault_admin_password{,_general}`, `vault_samba_secret`, `vault_ubuntu_password`, `vault_user_volume_key`, `vault_secrets_volume_key`) in cleartext because they lived in auto-loaded `group_vars/{all,storage}/vault.yml`. Each secret now loads via play-scoped `vars_files` in the eight plays that consume it (`vault/general.yml`, `vault/storage.yml`), and both group_vars vault symlinks were removed, so inventory has nothing to decrypt regardless of the password. Also dropped the global `-e @vault/*.yml` injection from `run-playbook.sh`. Validated: no `vault_*` keys in `--host` dumps; both vault files decrypt/resolve via real per-play paths in `--check` runs.

### Infrastructure

- **GPU-VM guest driver survives kernel upgrades** (`linux-headers-virtual`). The vm1 driver loss on a restart was caused by guests tracking `linux-image-virtual` with no headers metapackage, so when unattended-upgrades staged a new kernel (6.8.0-124) the DKMS hook silently skipped the build and the reboot came up driverless. `incus-build-gpu-image.sh.j2` now installs `linux-headers-virtual` (covers the `ubuntu-cuda-vllm` layer too), so headers ride along with each kernel image and DKMS rebuilds at install time. Helper deployed to nv2/nv3 via `install-incus.yml`; images rebuilt on both hosts; live guests vm1 + vm2 fixed by hand. (Closed 2026-06-13: vm3 reconciled by the operator. A follow-up eval found Ubuntu's precompiled `linux-modules-nvidia-595-open-generic` could replace DKMS outright — same driver point-release, secure-boot-signed, kernel-tracking — tracked as an optional switch in `TODO.md`.)

- **incus VM DNS host-records.** Pinned incus VMs now get a permanent DNS record so name-based SSH doesn't depend on a live DHCP lease (incus only serves a VM's DNS record while it holds a current dnsmasq lease; a static IP reservation creates no record). `sync_dns_records()` in `vm_manager.py` reconciles every managed bridge's `host-record=` lines from the eth0 pins (called from launch/destroy/pin-ips; `ycluster vm sync-dns` for backfill); `install-incus.yml`'s resolver task now preserves host-record lines instead of clobbering them. Ownership split: the playbook owns resolver lines, `vm_manager` owns host-records. Verified on the dev cluster (lease-less pinned containers resolve; add/remove lifecycle; both ownership directions idempotent), then deployed to nv2/nv3/c1/c2 (nv2 regenerated vm2 + added the missing vm3 record, nv3 added vm1; bastion `getent` resolves vm2/vm3).

- **Frontend-node management cleanup** (after the S2 rollout touched dev1b). Frontend nodes (`f*`/dev1b) were second-class operationally; actionable gaps closed:
  - internal DNS: `/api/hosts` now also emits `.xc` records for `/cluster/nodes/frontend/*` (IP-registered nodes; hostname-registered ones already resolved). Frontend entries land in `/etc/static-hosts` and dnsmasq serves them; verified cluster-wide (`dev1b.xc` → public IP, ping + ssh reachable from a non-core node).
  - hostvars secrets: the rathole token no longer rides in inventory (H4), and the vaulted group_vars are play-scoped via `vars_files` with both symlinks removed, so `ansible-inventory --host` no longer decrypts any of them (commit `297d1c0`).
  - operator SSH config (`.yc` ssh_config) is out of scope — the operator maintains it locally.

- **Failed-systemd-units alert.** `SystemdUnitFailed` rule in `ycluster-alerts.yml.j2` (`node_systemd_unit_state{state="failed"} == 1` for 15m, warning). Benign offenders cleared by the new `monitoring/clear-benign-failed-units.yml` (generic, condition-detected: re-runs stale wait-online oneshots, installs a zz- drop-in making wait-online `--any -o routable`, disables networkd wait-online where it manages no links, masks failed `serial-getty@*` instances and host nvidia services on all-passthrough VM hosts). Boot-validated on nv1 (wait-online 120s-timeout-fail → 2.4s success). Caught a real failure within minutes of deployment (nv1's hand-rolled autossh-tunnel tunnel).

- **"core" definition consistency — residual cleanup.** The 2026-06-10 audit found three meanings of "core"; all resolved:
  - rathole SSH ingress made parametric per core node (`9169531`): server endpoints loop over `groups['core']`, client regex `^s(\d+)$`; s4 now gets ssh4/2204, scales to s5+. Fixed the crash loop caused by the `^s([123])$` hardcode in `rathole_config.py` vs `hosts: core`.
  - the `core`/`etcd` groups are now backed by a static FLOOR (s1–s3) in `inventory_boot.yml` plus a gitignored per-cluster `inventory_cluster.yml` for extra core nodes / larger quorum (`2d60a12`, `4ffe153`); the plugin still unions in dynamic s4+. Fixes the DR/bootstrap circular dependency and leaves the door open to grow etcd to 5.
  - Tang/Clevis unlock set now derived from `groups['etcd']` + `tang_port` at deploy time (`tang_servers` var); the two manager scripts became templates (`admin/templates/secrets-volume-manager.j2`, `storage/templates/user-rbd-manager.j2`); rendered output verified byte-identical to the old hardcode (no clevis rebind triggered). `CORE_NODE_IPS` removed — the netplan-failure fallback in `dhcp_server.py` now calls `determine_ip_from_hostname()`. The inventory plugin warns when live etcd membership differs from the static `etcd` floor.
