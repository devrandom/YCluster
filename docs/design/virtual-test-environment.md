# Virtual test environment

## Status
In progress. Substrate being stood up. This doc records the decisions and
the target shape; the existing `dev/` PXE harness is reused where it fits.

### Progress (updated 2026-06-04)
**Working end-to-end: a real 3-node etcd quorum runs in the containers via
the actual `install-etcd.yml`.** Verified: 3 members on 10.0.0.11-13, all
healthy, leader elected, write-on-s1 / read-on-s3 replicates.

Substrate (all volatile — `dev-cluster.sh up` recreates it each boot):
- Incus installed in the **template** (`apt -t bookworm-backports install
  incus`); daemon `incus admin init --minimal` (dir pool).
- Network `ycdev0` (10.0.0.1/24, NAT, no IPv6); profile `yc-node`
  (privileged; root disk; eth0 on ycdev0).
- Nodes s1/s2/s3 (core/etcd, .11/.12/.13) + c1 (compute, .51), all
  `images:ubuntu/24.04`, static-addressed, egress + full mesh OK.
- Ansible driven from the host via the `community.general.incus`
  connection (no SSH). Installed into the repo `venv/` (persists in
  /home); modern `community.general` pulled via `ansible-galaxy` (the
  Debian-apt one predates the incus connection plugin).

**Decision refinement:** dropped DHCP and cluster DNS — **static IPs +
`/etc/hosts`** (only DHCP/DNS-to-dnsmasq need the host INPUT chain, which
Qubes drops; everything else is bridge/FORWARD or `incus exec`).

#### Gotchas hit and how they're handled (all in `dev/cluster/`)
1. **Container egress dropped by Docker, not Qubes.** `table ip filter`
   FORWARD is `policy drop` (Docker, for the `dev/` harness); ycdev0
   traffic matches no Docker accept → dropped (a `drop` in any forward
   base chain wins over Qubes' `accept`). Fix: `ensure_firewall()` inserts
   `iifname/oifname "ycdev0" accept` into **`DOCKER-USER`**. NAT itself was
   already fine (Incus `pstrt.ycdev0` masquerade + ip_forward).
2. **`netplan apply` fails in containers** (`udevadm` with no udev). Write
   a systemd-networkd `.network` directly and `rm` the image's netplan
   (both `/etc/netplan/*.yaml` and the generated `/run` unit, which sorts
   first and would DHCP).
3. **Vault.** Only `group_vars/storage/vault.yml` is vaulted; our nodes
   don't need it, so the dev inventory has **no `storage` group**. The
   playbooks we exercise target etcd/core/managed/compute. (Aside: on
   s3.yc that file is a symlink to `../../vault`; in this checkout it's a
   materialized vault file — local layout quirk, unrelated.)
4. **Incus connection auth.** The Ansible plugin runs `incus` as the
   invoking user; rather than an incus-admin group re-login,
   `ensure_access()` ACLs the socket (`setfacl u:<user>:rw`).
5. **`ansible_incus_host` pin.** The plugin's `remote_addr` also accepts
   `ansible_host` (which we set to the IP for playbook logic) and it would
   win — so pin `ansible_incus_host: "{{ inventory_hostname }}"`.
6. **Latent `install-etcd.yml` bug surfaced.** On a fresh 3-node bootstrap
   the "add member to existing cluster" task is skipped, but Ansible 2.19
   templates its `delegate_to: "{{ working_etcd_host }}"` before `when:`,
   erroring on the undefined var. Worked around with a harmless
   `working_etcd_host` default in the dev inventory; **worth fixing
   upstream** (`| default(omit)`-style).
7. **etcd auto-start race.** The apt package auto-starts etcd with
   defaults (localhost, data in `/var/lib/etcd/default/`) before the
   playbook configures `ETCD_DATA_DIR=/var/lib/etcd`. A clean run forms
   quorum (orphaned `default/` ignored); but re-provisioning over a
   half-configured etcd needs a wipe (`systemctl stop etcd; rm -rf
   /var/lib/etcd/* /etc/default/etcd`) — or just `dev-cluster.sh reset`.

#### Next steps
1. Verify keepalived VIP (gateway 10.0.0.254 / storage 10.0.0.100) +
   failover in the privileged containers — the other phase-1 risk.
2. Add admin-api + leader-election playbooks to `site-dev.yml` (supplying
   dev secrets, no production vault).
3. Confirm a from-blank `reset` → `site-dev.yml` run is clean (the etcd
   work above used a state-wipe replay, not fresh containers).
4. Run the etcd-hardening changes against it.

Repo changes for this work are uncommitted.

## Goal
A throwaway, local cluster to test infrastructure changes (the immediate
driver is the etcd-access hardening in
[`etcd-access-hardening.md`](etcd-access-hardening.md)) without touching
the real cluster: etcd quorum, leader election, admin-api, and keepalived
VIP failover, exercised by the real playbooks.

## What already exists
`dev/` is a **bootstrap harness**, not a cluster:
- `dev/docker-compose.yaml` — single-container etcd + live-mounted
  admin-api + nginx/squid/chrony, for the PXE/autoinstall + allocation
  path with one machine netbooting against it.
- `dev/` dnsmasq + grub + autoinstall + `dev/ansible/` dev inventory.

It has no etcd quorum, no leader election, no VIP failover, no Ceph — so
it can't exercise what the hardening work (or most infra changes) touch.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Where it runs | **Local** (this Qubes Debian-12 AppVM) | RAM balloons as needed; no extra hardware |
| Substrate | **Incus system containers** | No `/dev/kvm` here (Qubes AppVM, no nested virt) — containers share the host kernel and need none. Matches the cluster's own tooling (`install-incus.yml`, `ycluster vm`). True systemd-init nodes, static IPs, cheap snapshot/restore for `reset`. |
| Fidelity | **Core-only, no Ceph** | Ceph (snap + kernel modules + real block devices) is the one thing system containers can't do; excluding it is what makes containers viable. Enough to test the etcd work fully. |

`systemd-nspawn` (`systemd-container` pkg) is the fallback if Incus proves
awkward inside the AppVM.

### Why not VMs / why local works after all
Initial probe found no `/dev/kvm` and no `vmx`/`svm` (Qubes AppVM, no
nested virt) and 6 GB RAM — which rules out **VMs** locally, not
containers. System containers don't need KVM, and RAM is not a hard cap
here. Host capability confirmed: cgroup v2, unprivileged userns enabled
(`max_user_namespaces=15445`), `subuid/subgid` mapped, passwordless sudo,
Incus 6.0.4 in bookworm-backports.

### Qubes persistence caveat
An AppVM's root FS resets on reboot; packages installed via apt in the
AppVM don't survive a reboot. For a durable setup, install Incus in the
**template**. The cluster definition itself lives in the repo
(`dev/cluster/`) so it's reproducible regardless.

## Target shape

Nodes (Ubuntu 24.04 system containers, matching the real node OS):

| Container | Role | IP |
|---|---|---|
| s1, s2, s3 | core (etcd quorum, admin-api, leader election, keepalived) | 10.0.0.11/12/13 |
| c1 | compute (admin-api client, `ycluster vm` etcd path) | 10.0.0.51 |
| (VIPs) | gateway / storage, float via keepalived | 10.0.0.254 / 10.0.0.100 |

Network: a dedicated Incus managed bridge (`ycdev0`, host 10.0.0.1/24,
NAT on) so nothing collides with the host or the real cluster. Static IPs
per container; VIPs are secondary addresses assigned by keepalived.

Containers run **privileged** (`security.privileged=true`) so keepalived
gets `NET_ADMIN` and VRRP multicast works on the bridge.

No PXE/autoinstall: containers are launched from the Ubuntu image,
minimally prepped (python3 for Ansible), then provisioned by the real
playbooks. The bootstrap/autoinstall path stays the domain of the
existing `dev/` docker harness.

Ansible reaches nodes via the `community.general.incus` connection plugin
(exec into the container as root — no SSH key dance), with a static boot
inventory listing s1/s2/s3/c1. A curated `site-dev.yml` imports only the
container-safe subset of playbooks (etcd, locale, ycluster package,
admin-api/web-services, leader election, gateway/storage VIP) — explicitly
**not** Ceph, monitoring, macOS, or the autoinstall bits.

## Layout (repo)
```
dev/cluster/
  dev-cluster.sh   # up | down | reset | status | exec <node>; idempotent.
                   #   Creates network/profile, applies the DOCKER-USER +
                   #   socket-ACL fixes, launches nodes, static-configures them.
  ansible.cfg      # inventory + incus connection defaults (run from here)
  inventory.yml    # static s1-s3/c1; incus connection; no `storage` group
  site-dev.yml     # top-level wrapper that import_playbooks the real,
                   #   container-safe subset (so prod group_vars aren't loaded)
```
Run Ansible from `dev/cluster/` with the repo venv:
`../../venv/bin/ansible-playbook site-dev.yml`. (`Makefile` targets can wrap
these later.)

## Phasing
1. **Substrate (now):** install Incus, create `ycdev0`, launch s1-s3/c1,
   confirm networking + a real 3-node etcd quorum (via `install-etcd.yml`)
   and keepalived VIP failover in containers. This is the riskiest part.
2. **Provisioning:** wire the static inventory + `site-dev.yml`; get
   admin-api + leader election running cluster-wide.
3. **Use it:** run the etcd-hardening changes against it (admin-api
   health-check gating, inventory-collect delegation, firewall/mTLS).
4. **Later (optional):** full-stack tier on real KVM VMs (Ceph/PG/Qdrant/
   Open-WebUI) hosted on a cluster Incus host.

## Open questions
- keepalived VRRP fidelity in privileged containers (multicast on the
  Incus bridge) — validate in phase 1.
- Which playbook tasks assume bare-metal facts (disks, NICs, snap) and
  need guarding/stubbing to run in containers — discover while wiring
  `site-dev.yml`.
