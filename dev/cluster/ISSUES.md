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
### Pebble placement — moved to dedicated container

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

**Previous placement (f1):** Pebble ran on f1 (frontend proxy), so f1
served two roles: frontend proxy + test CA server. f1's firewall needed
a rule for port 14000 (Pebble's HTTPS listener).

**New placement (dedicated `pebble` container at `10.0.0.42`):**
Pebble now runs on a dedicated container. This:
1. Keeps f1 focused on its proxy role — no firewall changes needed on f1
2. Better simulates the "external CA on the internet" — a separate entity
3. Lets Pebble's playbook own its own firewall config

The Pebble container needs to reach f1:80 for HTTP-01 validation, and
be reachable from certbot on port 14000. Both work since it's on the
cluster network.

**Files changed:**
- `dev-cluster.sh` — added `pebble` to NODES, OCTET (42), HOSTS_BLOCK
- `inventory.yml` — added `pebble` group with `pebble` host at 10.0.0.42
- `acme-dev.yml` — first play now `hosts: pebble` (was `frontend`); removed
  UFW rule (no firewall between containers on the same bridge); dnsmasq
  fixture now points `pebble.xc` at the pebble container's IP
- `system-test.sh` — section 15 now checks pebble on the `pebble` container
  (was `f1`); added `[pebble]=10.0.0.42` to IP array

---

## Buildhost — delegated binary builds

**Problem:** Build steps (rathole, open-webui) ran on `localhost` — the
ansible controller. The controller may be differently configured from the
cluster machines (different OS, architecture, toolchain versions). Binaries
built on the wrong arch or with wrong glibc won't run on the cluster.

**Fix:** Added a `buildhost` var (default `s1`) and `build_cache_dir` var
(default `./tmp/build-cache`) to the inventory. Build playbooks now run on
the buildhost and install playbooks fetch the binary from the buildhost to
the controller's `build_cache_dir`, then copy to targets.

**Files changed:**
- `config/ansible/group_vars/all/main.yml` — added `buildhost: s1`, `build_cache_dir: ./tmp/build-cache`
- `dev/cluster/inventory.yml` — same vars for dev cluster
- `config/ansible/app/build-rathole.yml` — runs on localhost, delegates each task to `{{ buildhost }}`
- `config/ansible/app/build-open-webui.yml` — uses `hosts: "{{ buildhost }}"`
- `config/ansible/app/install-rathole.yml` — stat on buildhost, fetch to controller, copy to target
- `config/ansible/app/install-rathole-server.yml` — same fetch-from-buildhost pattern
- `config/ansible/admin/install-vm-bastion.yml` — same fetch-from-buildhost pattern

**`import_playbook` caveat:**
`import_playbook` evaluates `hosts` at parse time, before inventory vars are
loaded. So `build-rathole.yml` uses `hosts: localhost` (static) and delegates
each task to `{{ buildhost }}` at runtime. `build-open-webui.yml` uses
`hosts: "{{ buildhost }}"` directly because it's not imported via
`import_playbook` in the dev flow (it uses `include` semantics).

**`become` on delegated tasks:**
The `apt install` task in `build-rathole.yml` needs `become: yes` on the
delegated host (the buildhost). Without it, apt fails because the controller
runs as unprivileged user. The `become: yes` is on the task, not the play,
so it only applies to the delegated task.

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
| Buildhost var | `group_vars/all/main.yml`, `dev/cluster/inventory.yml` | Both |
| Buildhost delegation | `build-rathole.yml`, `install-rathole*.yml`, `install-vm-bastion.yml` | Both |

## Next Steps

1. ~~Move Pebble to a dedicated VM~~ — done (pebble container at 10.0.0.42)
2. **Decide on section 10** — skip in rootless mode, or run dev cluster with SUDO=sudo
3. **Run full system test** — verify all fixes work together
