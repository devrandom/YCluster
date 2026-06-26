# Running Incus rootless (non-root user access)

This covers how to give an unprivileged user the ability to run Incus
containers/VMs without `sudo`. Note the model: **`incusd` still runs as root** —
you are delegating *sandboxed access* to a root-owned daemon, not running a
fully rootless daemon the way Podman does.

What you can and can't get:

- **Unprivileged containers** (uid-mapped, root-in-container ≠ root-on-host):
  yes, this is the default.
- **Non-root users running containers** via delegated access: yes, via
  `incus-user` (below).
- **A daemon with zero host privilege** like rootless Podman: no — `incusd` is a
  root daemon (it manages bridges, dnsmasq, storage pools, cgroups, mounts).

If your goal is "let a normal user spin up containers without sudo,"
`incus-user` is the answer. If it's "no root anywhere on the host," Incus is the
wrong tool.

## 1. Install & init the daemon (as root, once)

```bash
# Debian/Ubuntu — incus-user ships with the incus package
sudo apt install incus
```

Then initialize the daemon (creates a storage pool + bridge). `incus admin init`
is **interactive** by default — it asks about clustering, storage backend,
bridge, network availability, etc. Three ways to run it:

**Interactive** — answer the prompts. Sane answers for a dev host: no
clustering, `dir` storage backend (simplest, no loop device), a new `incusbr0`
bridge with `ipv4.address: auto` and `ipv6.address: none`, not available over
the network. Say **yes** to the final "print preseed" prompt — it dumps the YAML
equivalent you can reuse below.

```bash
sudo incus admin init
```

**All defaults, no questions:**

```bash
sudo incus admin init --auto
```

**Reproducible preseed** (what the interactive run prints at the end):

```bash
cat <<'EOF' | sudo incus admin init --preseed
config: {}
networks:
- name: incusbr0
  type: bridge
  config:
    ipv4.address: auto
    ipv6.address: none
storage_pools:
- name: default
  driver: dir
profiles:
- name: default
  devices:
    eth0:
      name: eth0
      network: incusbr0
      type: nic
    root:
      path: /
      pool: default
      type: disk
EOF
```

`dir` is the simplest storage backend (just a directory, no loop file). Use
`zfs` instead if you want snapshots/clones, at the cost of a dedicated block
device or a loop-backed pool.

## 2. Enable the per-user socket

```bash
sudo systemctl enable --now incus-user.service
```

This backs a separate socket (`/var/lib/incus/unix.socket.user`). When a
non-admin user connects to it, `incus-user` auto-provisions a **restricted
project** scoped to that user (uid-mapped, limited devices, no host bridges by
default).

## 3. Group membership decides which socket you get

The access model is two groups:

| Group         | Socket      | Access level                          |
|---------------|-------------|---------------------------------------|
| `incus-admin` | main socket | full root-equivalent admin            |
| `incus`       | user socket | restricted, sandboxed per-user project |

For rootless-style access, add the user to **`incus`** (NOT `incus-admin`):

```bash
sudo usermod -aG incus alice
# alice logs out/in (or: newgrp incus) for the group to take effect
```

Now `alice` gets her own isolated project automatically:

```bash
incus launch images:debian/13 c1
incus list
```

…with no sudo and no ability to touch other users' or the host's resources.

## 4. (Optional) per-user resource limits

Limits for auto-created user projects are configured on the `incus-user` side
(e.g. `limits.instances`, `limits.disk`). The exact keys have shifted between
releases — check `man incus-user` for your version.

## 5. Verify subuid/subgid (for unprivileged mapping)

Containers in user projects are unprivileged, mapped via `/etc/subuid` and
`/etc/subgid` for the `root` user that runs `incusd`. The package usually sets
this up; confirm:

```bash
grep root /etc/subuid /etc/subgid
# root:1000000:1000000000  (or similar large range)
```

## Caveats

- **Networking**: restricted projects can't create host bridges. Users either
  share a managed network the admin pre-creates and grants, or use a
  `routed`/proxy NIC. This trips people up most often.
- **VMs vs containers**: both work, but VMs need more delegation; restricted
  projects lean toward containers.

## 6. Dev cluster bridge (`incusbr-1001`)

`setup-rootless-incus.sh` is the root-run, idempotent setup that produces the
network the dev cluster (`dev/cluster/`) expects. What it creates, as root:

- a `dir` storage pool named `default` (if `incus admin init` hasn't run yet)
- a per-user bridge **`incusbr-1001`** on **`10.0.0.1/16`**, NAT on, **DHCP off**
  — the dev cluster's DHCP leader is supposed to be the only answerer on the
  wire, so the bridge must not hand out leases
- nftables rules (needs root), installed as a boot-time `yc-dev-firewall`
  systemd oneshot (see below): a `DOCKER-USER` accept so the bridge can reach
  the internet past Docker's `FORWARD` policy-drop (without it, apt/curl in the
  nodes hang), plus a VRRP-multicast NAT exemption for keepalived.
- `incus-user.service` enabled (the per-user restricted socket)
- `dev` added to the `incus` group (restricted socket, *not* `incus-admin`)
- root's `/etc/subuid` + `/etc/subgid` mapping for uid-shifting
- the `user-1001` project granted `restricted.networks.access=incusbr-1001`

The bridge is created in the default project; the restricted `user-1001`
project can only *attach* containers to `incusbr-1001`, not list or reconfigure
it. From the user's view `incus network list` returns empty and `incus network
show incusbr-1001` 403s — that's expected, the bridge still works.

### Verified behavior (2026-06-26)

- `incus launch images:ubuntu/24.04 testnode` works from the `incus` group
  without sudo; the container attaches to `incusbr-1001` and gets a link-local
  IPv6 only (no IPv4, because DHCP is off by design).
- Manual static IPv4 inside the container works and reaches the gateway:
  `incus exec testnode -- ip addr add 10.0.0.11/24 dev eth0` → `ping 10.0.0.1`
  succeeds. So `incusbr-1001` is correctly on `10.0.0.0/16`.
- `ipv4.address` on the device is rejected unless paired with
  `security.ipv4_filtering=true`; even then the address didn't apply on this
  bridge — use `incus exec … ip addr add` instead, or let the dev cluster's
  `configure_net()` write the systemd-networkd static config.
- Storage volumes work in the user project: `incus storage volume create
  default testvol` / `… delete` round-trip cleanly. Listing storage *pools*
  403s (`Certificate is restricted`) — also expected for a restricted project.

### What still needs root (not covered by rootless access)

- Creating or reconfiguring networks — the `user-1001` project has
  `features.networks: "false"`, so `incus network create ycdev0` is rejected
  with `Network not allowed in project`. The dev cluster must use the
  pre-created `incusbr-1001`, not its own `ycdev0`.
- nftables firewall rules (`nft insert rule …`). Two distinct needs, **both
  root**, now handled once by `setup-rootless-incus.sh` (`ensure_firewall`):
  - **Egress (required when Docker is installed).** Docker sets the filter
    `FORWARD` policy to **DROP** and inserts accept rules for *its own* bridges
    only. A non-Docker bridge like `incusbr-1001` then falls through to that
    DROP, so containers can reach the bridge gateway (`10.0.0.1`) but **not the
    internet** — apt/curl hang. Verified on this host: a Docker container
    reaches `http://1.1.1.1/` (HTTP 301) while an incus container on
    `incusbr-1001` times out. The fix is one accept in Docker's `DOCKER-USER`
    chain for the bridge. There is no rootless way to edit that chain.
  - **VRRP multicast.** Exempt `224.0.0.0/4` from NAT so keepalived adverts
    don't collide (best-effort; keepalived in containers is a rough test).
  These chains are torn down and recreated each boot (by Docker/incus), so the
  setup installs a `yc-dev-firewall.service` oneshot (`/usr/local/sbin/yc-dev-firewall`)
  that reapplies them after `docker`/`incus` come up — no per-boot chore. Remove
  it with `sudo dev/setup-rootless-incus.sh --teardown`.
- Relocating `/var/lib/incus` onto `/rw` via a bind mount (`ensure_persist`) —
  needs root for `mount --bind` and the Qubes bind-dir config. Skip it; the
  daemon is already initialised and the pool is `dir` on the host filesystem.

### Running the dev cluster without sudo

`dev/cluster/dev-cluster.sh` defaults to rootless: `SUDO` is empty, every
`incus` call runs as the `incus`-group user, and the root-only steps
(`ensure_persist`, `ensure_daemon` init, `ensure_firewall`, `ensure_network`
creation) are no-ops that trust `setup-rootless-incus.sh` to have done the
host prep. So the workflow is:

1. **Once, as root:** `sudo dev/setup-rootless-incus.sh` — creates the
   `incusbr-1001` bridge (`10.0.0.0/16`, NAT on, DHCP off), the storage pool,
   the `DOCKER-USER` egress accept + VRRP multicast NAT exemption, the
   per-user socket/group/subids, and grants the `user-1001` project access to
   the bridge. Re-run after a reboot (the nft rules aren't persistent).
2. **Then, rootless:** `dev/cluster/dev-cluster.sh up` (no sudo) stands up the
   nodes against `incusbr-1001`.

Set `SUDO=sudo` to fall back to the old root-run `ycdev0` path (Qubes).
