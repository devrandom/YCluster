#!/usr/bin/env bash
#
# dev-cluster.sh — stand up a throwaway local ycluster on Incus system
# containers for testing infra changes (see
# docs/design/virtual-test-environment.md).
#
# Everything here is idempotent and volatile: Incus daemon state lives on
# the Qubes AppVM root, which resets on reboot, so `up` re-creates the
# whole thing from scratch each session.
#
# Usage:
#   ./dev-cluster.sh up        # init incus + network + apt cache + nodes (idempotent)
#   ./dev-cluster.sh down      # delete the node containers (keep net/profile/cache)
#   ./dev-cluster.sh reset     # down + recreate (warm apt cache reused)
#   ./dev-cluster.sh purge     # down + also delete the persistent apt cache
#   ./dev-cluster.sh status    # show nodes
#   ./dev-cluster.sh exec s1 [cmd...]   # shell/command in a node
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- config --------------------------------------------------------------
SSH_KEY="$SCRIPT_DIR/.ssh/id_dev"   # generated on first `up`; gitignored
SSH_CONFIG="$SCRIPT_DIR/.ssh/config"        # generated; `ssh -F` into dev nodes
KNOWN_HOSTS="$SCRIPT_DIR/.ssh/known_hosts"  # local, scrubbed each `up`
# Incus state defaults to the volatile AppVM root (resets on reboot, small).
# We relocate it to the persistent private volume (/rw) via a Qubes bind-dir.
STATE_DIR=/var/lib/incus
PERSIST_DIR=/rw/bind-dirs/var/lib/incus
BIND_CONF=/rw/config/qubes-bind-dirs.d/50-incus.conf
NETWORK=ycdev0
SUBNET=10.0.0.1/24
PROFILE=yc-node
IMAGE=images:ubuntu/24.04
DNS="1.1.1.1 8.8.8.8"

# Persistent apt cache (apt-cacher-ng). A substrate container — survives
# `down`/`reset` so recreating nodes pulls debs from the warm cache instead
# of the internet. Removed only by `purge`.
CACHE=aptcache
CACHE_IP=10.0.0.2
CACHE_PORT=3142

# node -> last octet (10.0.0.X). Core = s1-s3, compute = c1, frontend = f1.
NODES=(s1 s2 s3 c1 f1)
declare -A OCTET=( [s1]=11 [s2]=12 [s3]=13 [c1]=51 [f1]=41 )

# /etc/hosts seeded into every node (no cluster DNS — we use static hosts).
HOSTS_BLOCK=$(cat <<'EOF'
10.0.0.11 s1 s1.xc
10.0.0.12 s2 s2.xc
10.0.0.13 s3 s3.xc
10.0.0.51 c1 c1.xc
10.0.0.41 f1 f1.xc
10.0.0.2 aptcache
10.0.0.100 admin.xc registry.xc inference.xc storage.xc
10.0.0.254 gateway.xc
EOF
)

INCUS() { sudo incus "$@"; }

# --- substrate -----------------------------------------------------------
ensure_persist() {
  # Move /var/lib/incus (daemon DB + image cache + dir storage pool) off the
  # volatile AppVM root onto the persistent private volume (/rw), so the dev
  # cluster survives reboots and gets /rw's large disk instead of the ~2GB
  # free on root. `reset` stays the explicit throwaway; reboots no longer
  # cost a full reprovision.
  if [ ! -f "$BIND_CONF" ]; then
    sudo mkdir -p "$(dirname "$BIND_CONF")"
    echo "binds+=('$STATE_DIR')" | sudo tee "$BIND_CONF" >/dev/null
    echo "[*] $BIND_CONF written (Qubes binds $STATE_DIR from /rw at boot)"
  fi
  # Already on /rw — via the Qubes boot-time mount, or a prior run this boot.
  if mountpoint -q "$STATE_DIR"; then return 0; fi
  # First time this boot (before Qubes' native bind takes effect): stop incus,
  # then bind /rw over the root copy. We start clean rather than seeding — the
  # ensure_* steps rebuild the minimal state idempotently, and the hidden root
  # copy is reclaimed at the next reboot (root is volatile).
  echo "[*] relocating $STATE_DIR -> $PERSIST_DIR (until next reboot makes it native)"
  sudo systemctl stop incus.service incus.socket
  sudo mkdir -p "$PERSIST_DIR"
  sudo mount --bind "$PERSIST_DIR" "$STATE_DIR"
}

ensure_daemon() {
  sudo systemctl start incus.socket incus.service
  if ! INCUS storage list --format csv >/dev/null 2>&1 || \
     ! INCUS storage list --format csv 2>/dev/null | grep -q .; then
    echo "[*] incus admin init --minimal"
    INCUS admin init --minimal
  fi
}

ensure_firewall() {
  # ycdev0 egress dies in Docker's FORWARD policy-drop unless we allow it
  # in DOCKER-USER (Docker evaluates it first, reserves it for user rules).
  # See design doc. INPUT (DHCP/DNS to host) stays dropped by Qubes — we
  # don't use it (static IPs + /etc/hosts).
  for d in iifname oifname; do
    if ! sudo nft -a list chain ip filter DOCKER-USER 2>/dev/null | grep -q "$d \"$NETWORK\""; then
      sudo nft insert rule ip filter DOCKER-USER $d "\"$NETWORK\"" accept
      echo "[*] DOCKER-USER: allow $d $NETWORK"
    fi
  done
  # Keepalived's VRRP adverts (multicast 224.0.0.18) otherwise hit the
  # catch-all masquerades in BOTH the incus and qubes postrouting chains
  # (dst is outside 10.0.0.0/24): every node's adverts NAT to the bridge
  # address and collide on one conntrack tuple, so only one node's adverts
  # survive -> VRRP split-brain (two MASTERs, duplicate VIP). Exempt
  # multicast from NAT in both chains.
  if ! sudo nft list chain inet incus "pstrt.$NETWORK" 2>/dev/null | grep -q "224.0.0.0/4"; then
    sudo nft insert rule inet incus "pstrt.$NETWORK" ip daddr 224.0.0.0/4 return
    echo "[*] pstrt.$NETWORK: multicast exempt from masquerade"
  fi
  if ! sudo nft list chain ip qubes postrouting 2>/dev/null | grep -q "224.0.0.0/4"; then
    sudo nft insert rule ip qubes postrouting ip daddr 224.0.0.0/4 return
    echo "[*] qubes postrouting: multicast exempt from masquerade"
  fi
}

ensure_sshkey() {
  # Ansible connects over SSH (faithful to the real cluster; also makes the
  # synchronize/rsync-based playbooks work, which the incus-exec connection
  # can't). Generate a throwaway keypair once.
  if [ ! -f "$SSH_KEY" ]; then
    mkdir -p "$(dirname "$SSH_KEY")"; chmod 700 "$(dirname "$SSH_KEY")"
    ssh-keygen -t ed25519 -N '' -f "$SSH_KEY" -C ycluster-dev >/dev/null
    echo "[*] generated dev ssh key $SSH_KEY"
  fi
  # setup-web-services.yml publishes {{ bootstrap_files_dir }}/ansible_ssh_key.pub;
  # in the dev env bootstrap_files_dir points at this .ssh dir, so expose the
  # dev pubkey under that name.
  cp -f "${SSH_KEY}.pub" "$(dirname "$SSH_KEY")/ansible_ssh_key.pub"
}

ensure_sshconfig() {
  # A self-contained ssh config so you can `ssh -F dev/cluster/.ssh/config s1`
  # (or add `Include .../dev/cluster/.ssh/config` to ~/.ssh/config) with no
  # host-key prompts/warnings. Nodes are recreated on reset (host keys change),
  # so we keep a LOCAL known_hosts and scrub it each `up`; LogLevel ERROR hides
  # the "Permanently added" noise. ansible.cfg uses the same key + options.
  : > "$KNOWN_HOSTS"
  { echo "# Generated by dev-cluster.sh — do not edit. ssh -F this file."
    echo "Host ${NODES[*]} aptcache"
    echo "  User root"
    echo "  IdentityFile $SSH_KEY"
    echo "  IdentitiesOnly yes"
    echo "  UserKnownHostsFile $KNOWN_HOSTS"
    echo "  StrictHostKeyChecking accept-new"
    echo "  LogLevel ERROR"
    for n in "${NODES[@]}"; do
      echo "Host $n"; echo "  HostName 10.0.0.${OCTET[$n]}"
    done
    echo "Host aptcache"; echo "  HostName $CACHE_IP"
  } > "$SSH_CONFIG"
  echo "[*] wrote ssh config $SSH_CONFIG (ssh -F $SSH_CONFIG s1)"
}

ensure_network() {
  if ! INCUS network show "$NETWORK" >/dev/null 2>&1; then
    echo "[*] create network $NETWORK"
    INCUS network create "$NETWORK" ipv4.address="$SUBNET" ipv4.nat=true ipv6.address=none
  fi
}

ensure_profile() {
  if ! INCUS profile show "$PROFILE" >/dev/null 2>&1; then
    echo "[*] create profile $PROFILE"
    INCUS profile create "$PROFILE"
    INCUS profile set "$PROFILE" security.privileged=true
    INCUS profile device add "$PROFILE" root disk path=/ pool=default
    INCUS profile device add "$PROFILE" eth0 nic network="$NETWORK"
  fi
  # docker-in-container (app-dev.yml: authentik). Applies on node (re)start.
  if [ "$(INCUS profile get "$PROFILE" security.nesting)" != "true" ]; then
    echo "[*] enable security.nesting on $PROFILE"
    INCUS profile set "$PROFILE" security.nesting=true
  fi
}

# --- nodes ---------------------------------------------------------------
wait_ready() {  # wait for systemd inside the container to be usable
  local n=$1 i
  for i in $(seq 1 30); do
    if INCUS exec "$n" -- test -S /run/dbus/system_bus_socket 2>/dev/null \
       || INCUS exec "$n" -- systemctl is-system-running >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
}

disable_apt_timers() {
  # Disable the boot-time apt jobs up front so they never race provisioning
  # for the dpkg lock. Guard by existence (the minimal image may not ship
  # all of these) — a conditional, not error-swallowing.
  local n=$1 unit
  for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
    if INCUS exec "$n" -- systemctl list-unit-files "$unit" --no-legend | grep -q .; then
      INCUS exec "$n" -- systemctl disable --now "$unit"
    fi
  done
}

configure_net() {
  # Static networkd config written directly. We avoid netplan ("netplan
  # apply" calls udevadm, which has no running udev in a container) and DHCP
  # (dnsmasq sits behind Qubes' dropped INPUT chain). Drop the image's
  # cloud-init netplan (both the source yaml and the already-generated /run
  # unit, which otherwise sorts before ours and DHCPs).
  local n=$1 ip=$2
  INCUS exec "$n" -- sh -c 'rm -f /etc/netplan/*.yaml /run/systemd/network/*netplan*'
  INCUS exec "$n" -- sh -c "cat > /etc/systemd/network/10-yc.network" <<EOF
[Match]
Name=eth0

[Network]
Address=${ip}/24
Gateway=10.0.0.1
$(for d in $DNS; do echo "DNS=$d"; done)
EOF
  INCUS exec "$n" -- ip addr flush dev eth0 scope global
  INCUS exec "$n" -- systemctl enable systemd-networkd
  INCUS exec "$n" -- systemctl restart systemd-networkd
  # Deterministic resolv.conf (don't depend on resolved wiring in-container)
  INCUS exec "$n" -- sh -c "rm -f /etc/resolv.conf; for d in $DNS; do echo \"nameserver \$d\"; done > /etc/resolv.conf"
}

configure_node() {
  local n=$1 ip="10.0.0.${OCTET[$1]}"
  disable_apt_timers "$n"
  configure_net "$n" "$ip"
  # No apt recommends. The ycluster package's real deps are the explicitly
  # listed apt packages; recommends drag in build-essential, scipy, tk,
  # tcpdump/wireshark (via scapy/matplotlib/ipython) — ~640MB/node that
  # overflows the small AppVM root across 4 dir-pool copies. A deliberate
  # dev-env footprint cut (see design doc), not a playbook change.
  INCUS exec "$n" -- sh -c 'printf "APT::Install-Recommends \"false\";\nAPT::Install-Suggests \"false\";\n" > /etc/apt/apt.conf.d/99-no-recommends'
  # Route apt through the persistent apt-cacher-ng container so recreating
  # nodes pulls debs from cache, not the internet. http only — the Ubuntu
  # archives are http; https sources would just CONNECT-tunnel uncached.
  INCUS exec "$n" -- sh -c "printf 'Acquire::http::Proxy \"http://${CACHE_IP}:${CACHE_PORT}\";\n' > /etc/apt/apt.conf.d/00-aptcache-proxy"
  # Refresh the apt cache once at provision time. The stock image ships a
  # stale index, so exact-version installs 404 on since-superseded transitive
  # deps. Real nodes get this during autoinstall/bootstrap; do the equivalent
  # here so the playbooks find a fresh cache (no manual apt-get update).
  INCUS exec "$n" -- env DEBIAN_FRONTEND=noninteractive \
    apt-get update -o DPkg::Lock::Timeout=120 >/dev/null
  # /etc/hosts: drop any prior cluster entries, then append ours
  INCUS exec "$n" -- sh -c "sed -i '/\.xc/d; / s[0-9] /d; / c[0-9] /d' /etc/hosts; cat >> /etc/hosts" <<EOF
$HOSTS_BLOCK
EOF
  # SSH: install sshd and authorize the dev key for root (Ansible uses SSH).
  # stdout suppressed to keep apt progress quiet; stderr (real errors) shown.
  if ! INCUS exec "$n" -- test -x /usr/sbin/sshd; then
    INCUS exec "$n" -- env DEBIAN_FRONTEND=noninteractive \
      apt-get install -y -o DPkg::Lock::Timeout=120 openssh-server >/dev/null
  fi
  INCUS exec "$n" -- sh -c 'mkdir -p /root/.ssh && chmod 700 /root/.ssh'
  INCUS exec "$n" -- sh -c 'cat > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys' < "${SSH_KEY}.pub"
  INCUS exec "$n" -- systemctl enable --now ssh
}

ensure_cache() {
  # Persistent apt-cacher-ng container. Created on first `up`, survives
  # `down`/`reset` (it's not in NODES), so node provisioning after a reset
  # hits a warm deb cache. Its own apt-cacher-ng install is the only thing
  # that ever goes to the internet — and only once.
  if ! INCUS info "$CACHE" >/dev/null 2>&1; then
    echo "[*] create $CACHE ($CACHE_IP) — persistent apt cache"
    INCUS init "$IMAGE" "$CACHE" --profile "$PROFILE"
    INCUS config device override "$CACHE" eth0 ipv4.address="$CACHE_IP"
    INCUS start "$CACHE"
  elif [ "$(INCUS list "$CACHE" -c s -f csv)" != RUNNING ]; then
    INCUS start "$CACHE"
  fi
  wait_ready "$CACHE"
  disable_apt_timers "$CACHE"
  configure_net "$CACHE" "$CACHE_IP"
  # Install apt-cacher-ng directly (no proxy — this *is* the proxy).
  if ! INCUS exec "$CACHE" -- test -x /usr/sbin/apt-cacher-ng; then
    INCUS exec "$CACHE" -- env DEBIAN_FRONTEND=noninteractive \
      apt-get update -o DPkg::Lock::Timeout=120 >/dev/null
    INCUS exec "$CACHE" -- env DEBIAN_FRONTEND=noninteractive \
      apt-get install -y --no-install-recommends -o DPkg::Lock::Timeout=120 apt-cacher-ng >/dev/null
  fi
  INCUS exec "$CACHE" -- systemctl enable --now apt-cacher-ng
}

ensure_node() {
  local n=$1 ip="10.0.0.${OCTET[$1]}"
  if ! INCUS info "$n" >/dev/null 2>&1; then
    echo "[*] create $n ($ip)"
    INCUS init "$IMAGE" "$n" --profile "$PROFILE"
    INCUS config device override "$n" eth0 ipv4.address="$ip"
    INCUS start "$n"
  elif [ "$(INCUS list "$n" -c s -f csv)" != RUNNING ]; then
    INCUS start "$n"
  fi
  wait_ready "$n"
  configure_node "$n"
}

# --- commands ------------------------------------------------------------
cmd_up() {
  # firewall after network: the multicast NAT exemption needs the incus
  # pstrt chain, which exists only once the network does.
  ensure_persist; ensure_daemon; ensure_sshkey; ensure_sshconfig; ensure_network; ensure_firewall; ensure_profile
  ensure_cache   # before nodes — they apt through it
  for n in "${NODES[@]}"; do ensure_node "$n"; done
  echo; cmd_status
}

cmd_down()   { for n in "${NODES[@]}"; do if INCUS info "$n" >/dev/null 2>&1; then INCUS delete -f "$n"; echo "[*] deleted $n"; fi; done; }
cmd_reset()  { cmd_down; cmd_up; }
cmd_status() { INCUS list; }
cmd_exec()   { local n=$1; shift || true; INCUS exec "$n" -- "${@:-bash}"; }
# purge also drops the persistent apt cache (down keeps it).
cmd_purge()  { cmd_down; if INCUS info "$CACHE" >/dev/null 2>&1; then INCUS delete -f "$CACHE"; echo "[*] deleted $CACHE"; fi; }

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  reset)  cmd_reset ;;
  purge)  cmd_purge ;;
  status) cmd_status ;;
  exec)   shift; cmd_exec "$@" ;;
  *) echo "usage: $0 {up|down|reset|purge|status|exec <node> [cmd...]}" >&2; exit 1 ;;
esac
