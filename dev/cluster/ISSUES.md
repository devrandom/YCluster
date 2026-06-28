# Dev Cluster System Test — Open Issues

Status after fixes: **16/17 sections passing** (was 6/17).

## Remaining Failure

### Section 10 — incus vm lifecycle (c1)

`incus start smokevm` fails with:
```
newuidmap failed to write mapping "newuidmap: write to uid_map failed: Operation not permitted"
Failed to set up id mapping.
```

This is a **rootless Incus limitation**. Nested containers inside unprivileged
Incus containers can't write to `uid_map` because the parent container doesn't
have the `CAP_SETFCAP` / mapping privileges required. The `vm-launch` path
needs `security.privileged=true` on the nested container, which rootless mode
can't grant.

**Options:**
- Skip this section in rootless mode (mark as expected-fail)
- Run the dev cluster with `SUDO=sudo` (privileged mode) so nested Incus works
- Move the VM lifecycle test to a separate privileged container

---

## Resolved Issues

### Sections 12 & 16 — `/status` returned 500

The `/status` page is behind forward-auth (authentik outpost). The outpost
matches by Host header and only knows `admin.dev.test` / `app.dev.test`.
The test was hitting `/status` with `Host: <cluster-IP>`, which the outpost
didn't recognize → 404 → nginx returned 500.

**Fix:** Send `Host: admin.$domain` when accessing `/status` through f1 —
exactly what a browser sends on the real cluster. (`system-test.sh` sections
12 and 16.)

### runc 1.3.x regression (CVE-2025-52881)

**The error:**
```
runc create failed: unable to start container process: error during container init:
open sysctl net.ipv4.ip_unprivileged_port_start file: reopen fd 8: permission denied
```

**What changed in runc 1.3.x:**
runc 1.3.x added new code to read/set sysctls during container init. This code
uses "fd reopening" — reopening `/proc/self/fd/N` to access a file descriptor
that was already opened. In unprivileged/nested containers (LXC, Incus), this
fd reopening fails with EPERM because the container doesn't have permission
to access the host's procfs fd directory.

**The CVE connection:**
CVE-2025-52881 is titled "fd reopening causes issues with AppArmor profiles".
The same "fd reopening" mechanism that causes the AppArmor issue causes our
`permission denied` error. The fix for CVE-2025-52881 likely changed how fd
reopening works, but the underlying problem (fd reopening fails in
unprivileged containers) remains in runc 1.3.x.

**Why downgrade to 1.1.12:**
runc 1.1.12 is the last 1.1.x release. It predates the 1.3.x sysctl/fd-reopening
changes, so it doesn't have this regression. The 1.1.x branch is still
maintained (security fixes), so this is a safe downgrade.

**How runc is used:**
Docker calls runc (the OCI container runtime) to create and start each
container. When Docker asks runc to start a container, runc goes through
container init — setting up namespaces, cgroups, and sysctls. runc 1.3.x
added new code that uses "fd reopening" (`/proc/self/fd/N`) to read sysctls.
In unprivileged nested containers (Incus containers running Docker), this fd
reopening fails with EPERM.

runc tries to read `net.ipv4.ip_unprivileged_port_start` to determine if a
non-root container can bind to ports < 1024. The read fails because the fd
reopening technique doesn't work in nested containers.

**The `1002:1002` connection:**
With `user: "1002:1002"`, the container runs as non-root. runc needs to check
`ip_unprivileged_port_start` for non-root containers → fails. Without the
`user` directive, the container runs as root → runc skips the sysctl check →
container starts. So removing 1002:1002 was a workaround that made the
container run as root to bypass the broken sysctl read. The runc downgrade is
the correct fix because runc 1.1.12 doesn't use fd reopening — the sysctl
read works, and the container can run as user 1002 (non-root) with no
security regression.

**Conditional downgrade:**
The downgrade is gated on `docker_runc_downgrade` (default `false`):
- `install-docker-official.yml`: `when: docker_runc_downgrade | default(false)`
- `dev/cluster/inventory.yml`: `docker_runc_downgrade: true`

Production doesn't set this flag, so runc 1.3.x stays (it works fine on bare
metal / properly privileged LXC/Incus). The dev cluster sets it because Docker
runs nested in rootless Incus.

**The `1002:1002` fix:**
Reverted the removal — `user: "1002:1002"` stays in
`install-docker-registry.yml`. The runc downgrade to 1.1.12 fixes the sysctl
read, so the registry can run as user 1002 (non-root) with no security
regression.

### Pebble placement — it's not a certbot replacement

**Clarification:** Pebble is NOT a replacement for certbot. They serve
different roles:

| Component | Role | Production equivalent |
|-----------|------|---------------------|
| **Pebble** | ACME **CA server** (issues certificates) | Let's Encrypt's Boulder CA |
| **certbot** | ACME **client** (requests certificates) | certbot (same) |

So in the dev cluster:
- Pebble is a test ACME **CA server** (replaces Let's Encrypt's Boulder)
- certbot is the ACME **client** (requests certificates) — same as production
- The HTTP-01 challenge flows: Pebble → f1:80 → rathole tunnel → leader webroot

**Current placement (f1):**
f1 is the frontend proxy (Rathole server, public edge). Pebble currently runs
on f1, which means:
- f1's firewall needs a rule for port 14000 (Pebble's HTTPS listener)
- f1 serves two roles: frontend proxy + test CA server

**Proposed placement (dedicated VM):**
Move Pebble to a dedicated VM (e.g. `pebble` container at `10.0.0.42`). This
would:
1. Keep f1 focused on its proxy role — no firewall changes needed
2. Better simulate the "external CA on the internet" — a separate entity
3. Let Pebble's playbook own its own firewall config

The Pebble VM would need to reach f1:80 for HTTP-01 validation, and be
reachable from certbot on port 14000. Both work if it's on the cluster network.

---

## Summary of Fixes Applied

| Fix | Files | Dev/Prod |
|-----|-------|----------|
| ARP for VIPs | `dev-cluster.sh` (ipv4_filtering=false) | Dev only |
| runc downgrade (conditional) | `install-docker-official.yml` | Dev only (`docker_runc_downgrade: true`) |
| Inventory hostname | `inventory.py`, `app.py` | Both |
| Pebble firewall | `acme-dev.yml` | Dev only |
| sudo → incus | `system-test.sh` | Dev only |
| 1002:1002 reverted | `install-docker-registry.yml` | Both (runc downgrade fixes it) |
| `/status` Host header | `system-test.sh` (sections 12, 16) | Dev only |

## Next Steps

1. **Move Pebble to a dedicated VM** — keep f1 focused on its proxy role
2. **Decide on section 10** — skip in rootless mode, or run dev cluster with SUDO=sudo
3. **Run full system test** — verify all fixes work together

---

## Build Host — stop building on localhost

**Current state:** Build steps (rathole, local-ai-proxy, Go binaries) run
on `localhost` — the operator's development machine. Ansible delegates build
tasks to `localhost` and the resulting binaries are copied/scoped to the
cluster.

**Problem:** The operator's machine may be differently configured from the
cluster machines:
- Different OS / architecture (e.g. macOS vs Linux, arm64 vs amd64)
- Different Go / Rust / toolchain versions
- Different libraries / headers available
- Binaries built on the wrong arch or with wrong glibc won't run on the cluster

**Proposal:** Choose a **buildhost** — a dedicated machine (or one of the
cluster nodes) that has the same OS/arch/toolchain as the cluster targets.
All build steps should delegate to the buildhost instead of `localhost`.

**Options:**
- Use one of the core/storage nodes as the buildhost (same OS, same arch)
- Use a separate dedicated build machine
- Use a container on the buildhost for reproducible builds

**Affected files:**
- `config/ansible/app/build-rathole.yml` — `delegate_to: localhost`
- `config/ansible/app/build-local-ai-proxy.yml` — `delegate_to: localhost`
- Any other playbook that builds binaries locally

**Migration:**
1. Define `buildhost` in the inventory (default: `localhost`)
2. Replace `delegate_to: localhost` with `delegate_to: "{{ buildhost }}"`
3. Set `buildhost` to a cluster node or dedicated build machine
