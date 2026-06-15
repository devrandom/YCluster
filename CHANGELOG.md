# Changelog

Completed work, moved out of `TODO.md`. Newest first. One line per change;
detail lives in commits and code. Items with residual work stay in `TODO.md`.

## 2026-06-15

- **Clock-skew health band by node type.** Non-storage nodes now warn at 1s / crit at 10s (was 100ms / 1s); storage nodes keep the strict band since etcd/PostgreSQL need tight sync.

## 2026-06-14

- **Gateway-VIP hardening.** Health check now gauges fitness by live reachability to the frontends instead of one external HTTP GET (which flapped the VIP on any WAN blip); uplink-less nodes (s4) are excluded from holding the VIP.
- **Frontend host firewall.** The rathole and blackbox playbooks now manage a least-privilege ufw on frontend nodes (previously an implicit cloud security group).

## 2026-06-10

### Security & correctness

- **Verified review bugs B1–B8** (`docs/reviews/2026-06-09-codebase-review.md`) fixed and deployed: timing-safe key compare, allocation-CAS check, DHCP check-then-use race, Go-proxy retry scope, SQL parameterization, `wipe-etcd.yml` become fix, download checksums, non-silent inventory `except`.
- **Admin API S1 — privilege drop:** runs as a sudo-free `admin-api` user under waitress with `NoNewPrivileges`/`ProtectSystem=strict`; ceph check uses a read-only `client.admin-api` identity.
- **Admin API S2 — mutations moved to mTLS CLI:** removed unauthenticated mutating POSTs from `app.py`; drain/disable/enable/set-asset are now `ycluster` CLI writes to etcd; CA unified to `/etc/ycluster/ca`.
- **Admin API S3 — route param validation:** `@validated_hostname` on all hostname routes returns 400 before any etcd interpolation.
- **Drain-function auth — superseded** by the S2 mTLS-CLI move (status-page mutation UI deprecated).
- **H4 — `no_log` audit:** added `no_log` to every Ansible task handling a cleartext secret; rathole token read from etcd at task time so inventory dumps can't print it.
- **etcd access hardening — mTLS-only:** core etcd runs in `enforce` mode with no plaintext listeners (`docs/design/etcd-access-hardening.md`); dead RBD CA tree deleted after burn-in.
- **Reduced vault-secret exposure in `ansible-inventory`** (`297d1c0`): vaulted group_vars are play-scoped via `vars_files`, so `--host` dumps decrypt nothing.

### Infrastructure

- **GPU-VM guest driver survives kernel upgrades:** `incus-build-gpu-image.sh.j2` installs `linux-headers-virtual` so DKMS rebuilds the NVIDIA module when unattended-upgrades stages a new kernel.
- **incus VM DNS host-records:** pinned VMs get a permanent DNS record (`sync_dns_records()` in `vm_manager.py`) so name-based SSH doesn't depend on a live DHCP lease.
- **Frontend-node management cleanup:** `/api/hosts` emits `.xc` records for frontend nodes; rathole token and vaulted group_vars no longer ride in inventory.
- **Failed-systemd-units alert:** `SystemdUnitFailed` Prometheus rule plus `monitoring/clear-benign-failed-units.yml` to clear condition-detected benign offenders.
- **"core" definition consistency:** rathole SSH ingress parametric per core node; `core`/`etcd` backed by a static s1–s3 floor plus dynamic union; Tang/Clevis unlock set derived from `groups['etcd']`.
