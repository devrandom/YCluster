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

# --- config --------------------------------------------------------------
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
ensure_daemon() {
  sudo systemctl start incus.socket incus.service 2>/dev/null || true
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

ensure_access() {
  # Let the invoking user drive incus without sudo (needed by the Ansible
  # community.general.incus connection, which runs `incus` as that user).
  # ACL the socket so we don't depend on an incus-admin group re-login.
  sudo setfacl -m "u:$(id -un):rw" /var/lib/incus/unix.socket 2>/dev/null || true
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
  INCUS exec "$n" -- systemctl enable systemd-networkd >/dev/null 2>&1 || true
  INCUS exec "$n" -- systemctl restart systemd-networkd
  # Deterministic resolv.conf (don't depend on resolved wiring in-container)
  INCUS exec "$n" -- sh -c "rm -f /etc/resolv.conf; for d in $DNS; do echo \"nameserver \$d\"; done > /etc/resolv.conf"
  # /etc/hosts: drop any prior cluster entries, then append ours
  INCUS exec "$n" -- sh -c "sed -i '/\.xc/d; / s[0-9] /d; / c[0-9] /d' /etc/hosts; cat >> /etc/hosts" <<EOF
$HOSTS_BLOCK
EOF
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
  ensure_daemon; ensure_access; ensure_firewall; ensure_network; ensure_profile
  for n in "${NODES[@]}"; do ensure_node "$n"; done
  echo; cmd_status
}

cmd_down()   { for n in "${NODES[@]}"; do INCUS delete -f "$n" 2>/dev/null && echo "[*] deleted $n" || true; done; }
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
