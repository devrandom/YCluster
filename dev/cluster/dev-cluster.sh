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
#   ./dev-cluster.sh up        # init incus + network + nodes (idempotent)
#   ./dev-cluster.sh down      # delete the node containers (keep net/profile)
#   ./dev-cluster.sh reset     # down + recreate
#   ./dev-cluster.sh status    # show nodes
#   ./dev-cluster.sh exec s1 [cmd...]   # shell/command in a node
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- config --------------------------------------------------------------
SSH_KEY="$SCRIPT_DIR/.ssh/id_dev"   # generated on first `up`; gitignored
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

# node -> last octet (10.0.0.X). Core = s1-s3, compute = c1.
NODES=(s1 s2 s3 c1)
declare -A OCTET=( [s1]=11 [s2]=12 [s3]=13 [c1]=51 )

# /etc/hosts seeded into every node (no cluster DNS — we use static hosts).
HOSTS_BLOCK=$(cat <<'EOF'
10.0.0.11 s1 s1.xc
10.0.0.12 s2 s2.xc
10.0.0.13 s3 s3.xc
10.0.0.51 c1 c1.xc
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

configure_node() {
  local n=$1 ip="10.0.0.${OCTET[$1]}"
  # Disable the boot-time apt jobs up front so they never race provisioning
  # for the dpkg lock. Guard by existence (the minimal image may not ship
  # all of these) — a conditional, not error-swallowing.
  local unit
  for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
    if INCUS exec "$n" -- systemctl list-unit-files "$unit" --no-legend | grep -q .; then
      INCUS exec "$n" -- systemctl disable --now "$unit"
    fi
  done
  # No apt recommends. The ycluster package's real deps are the explicitly
  # listed apt packages; recommends drag in build-essential, scipy, tk,
  # tcpdump/wireshark (via scapy/matplotlib/ipython) — ~640MB/node that
  # overflows the small AppVM root across 4 dir-pool copies. A deliberate
  # dev-env footprint cut (see design doc), not a playbook change.
  INCUS exec "$n" -- sh -c 'printf "APT::Install-Recommends \"false\";\nAPT::Install-Suggests \"false\";\n" > /etc/apt/apt.conf.d/99-no-recommends'
  # Refresh the apt cache once at provision time. The stock image ships a
  # stale index, so exact-version installs 404 on since-superseded transitive
  # deps. Real nodes get this during autoinstall/bootstrap; do the equivalent
  # here so the playbooks find a fresh cache (no manual apt-get update).
  INCUS exec "$n" -- env DEBIAN_FRONTEND=noninteractive \
    apt-get update -o DPkg::Lock::Timeout=120 >/dev/null
  # Static networkd config written directly. We avoid netplan ("netplan
  # apply" calls udevadm, which has no running udev in a container) and DHCP
  # (dnsmasq sits behind Qubes' dropped INPUT chain). Drop the image's
  # cloud-init netplan so it stops generating a DHCP unit for eth0.
  # Remove the image's netplan (both the source yaml and the already-
  # generated /run unit, which otherwise sorts before ours and DHCPs).
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
  ensure_persist; ensure_daemon; ensure_sshkey; ensure_firewall; ensure_network; ensure_profile
  for n in "${NODES[@]}"; do ensure_node "$n"; done
  echo; cmd_status
}

cmd_down()   { for n in "${NODES[@]}"; do if INCUS info "$n" >/dev/null 2>&1; then INCUS delete -f "$n"; echo "[*] deleted $n"; fi; done; }
cmd_reset()  { cmd_down; cmd_up; }
cmd_status() { INCUS list; }
cmd_exec()   { local n=$1; shift || true; INCUS exec "$n" -- "${@:-bash}"; }

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  reset)  cmd_reset ;;
  status) cmd_status ;;
  exec)   shift; cmd_exec "$@" ;;
  *) echo "usage: $0 {up|down|reset|status|exec <node> [cmd...]}" >&2; exit 1 ;;
esac
