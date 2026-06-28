#!/usr/bin/env bash
#
# setup-rootless-incus.sh — root-run, idempotent setup that lets an
# unprivileged user drive Incus without sudo (the `incus-user` model from
# dev/README.md §1–§5), with a network suitable for the dev cluster
# (dev/cluster): a 10.0.0.0/16 bridge with Incus's own DHCP disabled so the
# cluster's DHCP leader is the only answerer on the wire.
#
# It implements, idempotently:
#   §1  install incus + ensure the daemon, a `default` (dir) storage pool,
#       and a per-user `incusbr-<uid>` bridge on 10.0.0.0/16, NAT on, DHCP off
#   §2  enable incus-user.service (the restricted per-user socket)
#   §3  add the user to the `incus` group (restricted socket, NOT incus-admin)
#   §5  ensure root's /etc/subuid + /etc/subgid mapping for uid-shifting
#   +   grant the auto-provisioned user-<uid> project access to the bridge
#
# The per-user project's default profile (created by incus-user) already
# references `incusbr-<uid>` and pool `default`; this script creates those so
# the restricted account works with no further config.
#
# Usage:
#   sudo ./setup-rootless-incus.sh [UID]              # set up (idempotent)
#   sudo ./setup-rootless-incus.sh --teardown [UID]   # undo what setup added
#     UID  target user's uid; defaults to dev's uid.
#
# --teardown reverses this script's own additions (firewall unit + live rules,
# the per-user bridge, the project's bridge grant, and group membership). It
# deliberately LEAVES the shared/generic bits in place: the incus install, the
# daemon, the `default` storage pool, and root's /etc/subuid+subgid ranges.

set -euo pipefail

# --- config --------------------------------------------------------------
MODE=setup
if [ "${1:-}" = "--teardown" ] || [ "${1:-}" = "teardown" ]; then
  MODE=teardown; shift
fi
TARGET_UID="${1:-$(id -u dev)}"
TARGET_USER="$(id -un "$TARGET_UID")"     # resolve uid -> name (fails loudly if unknown)
PROJECT="user-${TARGET_UID}"              # incus-user's per-user project name
NETWORK="incusbr-${TARGET_UID}"           # bridge its default profile references
NETADDR="10.0.0.1/16"                     # dev cluster lives on 10.0.0.0/16
POOL=default                              # pool the per-user profile references
SUBID_RANGE="1000000:1000000000"          # root's subuid/subgid range for uid-shift

log() { echo "[*] $*"; }

# --- preconditions -------------------------------------------------------
if [ "$(id -u)" != 0 ]; then
  echo "error: must run as root (sudo $0)" >&2
  exit 1
fi
log "target user: $TARGET_USER (uid $TARGET_UID), project $PROJECT, bridge $NETWORK"

# --- §1a install incus + QEMU (for --vm) --------------------------------
ensure_installed() {
  local need=()
  command -v incus >/dev/null 2>&1 || need+=(incus)
  # Incus VMs need qemu-system-x86 + OVMF firmware; the incus package
  # doesn't depend on QEMU, it just fails at launch time without it.
  [ -x /usr/bin/qemu-system-x86_64 ] || need+=(qemu-system-x86)
  [ -d /usr/share/OVMF ] || need+=(ovmf)
  if [ ${#need[@]} -gt 0 ]; then
    log "installing: ${need[*]}"
    apt-get update
    apt-get install -y "${need[@]}"
  fi
  # The incus daemon caches "QEMU not available" at startup. If QEMU is
  # now installed, restart the daemon so it re-detects it. This runs even
  # when nothing was installed (QEMU may have been added out-of-band).
  if [ -x /usr/bin/qemu-system-x86_64 ]; then
    log "restarting incus daemon to pick up QEMU"
    systemctl restart incus.service incus.socket
  fi
}

# --- §1b daemon + storage pool ------------------------------------------
ensure_daemon() {
  systemctl enable --now incus.socket incus.service
  # On a never-initialised daemon there is no storage pool; --minimal lays
  # down a usable baseline (default dir pool). Already-initialised daemons
  # have a pool and we skip it.
  if ! incus storage list --format csv 2>/dev/null | grep -q .; then
    log "incus admin init --minimal"
    incus admin init --minimal
  fi
}

ensure_pool() {
  if ! incus storage show "$POOL" >/dev/null 2>&1; then
    log "create storage pool $POOL (dir)"
    incus storage create "$POOL" dir
  fi
}

# --- §1c per-user bridge: 10.0.0.0/16, NAT on, DHCP off ------------------
ensure_network() {
  if ! incus network show "$NETWORK" >/dev/null 2>&1; then
    log "create network $NETWORK ($NETADDR, nat on, dhcp off)"
    incus network create "$NETWORK" \
      ipv4.address="$NETADDR" ipv4.nat=true ipv4.dhcp=false ipv6.address=none
    return 0
  fi
  # Reconcile each key so an existing bridge converges to the desired config.
  declare -A want=( [ipv4.address]="$NETADDR" [ipv4.nat]=true
                    [ipv4.dhcp]=false [ipv6.address]=none )
  for key in "${!want[@]}"; do
    if [ "$(incus network get "$NETWORK" "$key")" != "${want[$key]}" ]; then
      log "set $NETWORK $key=${want[$key]}"
      incus network set "$NETWORK" "$key" "${want[$key]}"
    fi
  done
}

# --- firewall: egress past Docker's FORWARD drop + VRRP multicast ---------
# Incus 6.0 has no bridge flag to adjust Docker's DOCKER-USER chain (incus only
# manages its own `inet incus` table via ipv4.firewall), so the operator must
# bridge the two firewalls. These nft rules need root AND are torn down every
# boot (Docker recreates DOCKER-USER; incus recreates pstrt.*), so rather than
# make this a per-boot chore we install a boot-time systemd oneshot that
# reapplies them after docker/incus come up. setup stays genuinely one-time.
FW_SCRIPT=/usr/local/sbin/yc-dev-firewall
FW_UNIT=/etc/systemd/system/yc-dev-firewall.service

ensure_firewall() {
  if ! command -v nft >/dev/null 2>&1; then
    log "nft not found; skipping firewall (egress may be blocked if Docker is present)"
    return 0
  fi
  # Single source of truth for the rules: a small idempotent helper, NETWORK
  # baked in at install time (the helper also accepts an override arg).
  log "install $FW_SCRIPT (egress + VRRP-multicast rules for $NETWORK)"
  cat > "$FW_SCRIPT" <<EOF
#!/usr/bin/env bash
# Installed by dev/setup-rootless-incus.sh. Re-allow egress for the ycluster
# dev bridge past Docker's FORWARD policy-drop, and exempt VRRP multicast from
# NAT. Run at boot by yc-dev-firewall.service because Docker/incus recreate
# these chains each boot. Idempotent. Override the bridge with \$1.
set -euo pipefail
NETWORK="\${1:-$NETWORK}"
command -v nft >/dev/null 2>&1 || { echo "nft not found" >&2; exit 0; }

# Docker sets the filter FORWARD policy to DROP and evaluates DOCKER-USER first,
# accepting only its own bridges. Without an accept for \$NETWORK there, NAT'd
# egress from the dev bridge is dropped and apt/curl inside the nodes hang.
# Only relevant when Docker is installed (the chain exists).
if nft list chain ip filter DOCKER-USER >/dev/null 2>&1; then
  for d in iifname oifname; do
    if ! nft -a list chain ip filter DOCKER-USER | grep -q "\$d \"\$NETWORK\""; then
      echo "[*] DOCKER-USER: allow \$d \$NETWORK"
      nft insert rule ip filter DOCKER-USER \$d "\"\$NETWORK\"" accept
    fi
  done
fi
# Keepalived VRRP adverts (multicast 224.0.0.18) otherwise hit incus's
# catch-all masquerade (dst outside the subnet): every node's adverts NAT to
# the bridge address and collide on one conntrack tuple -> VRRP split-brain.
# Exempt multicast from NAT. pstrt.\$NETWORK exists only once the bridge is up.
if nft list chain inet incus "pstrt.\$NETWORK" >/dev/null 2>&1 && \\
   ! nft list chain inet incus "pstrt.\$NETWORK" | grep -q "224.0.0.0/4"; then
  echo "[*] pstrt.\$NETWORK: multicast exempt from masquerade"
  nft insert rule inet incus "pstrt.\$NETWORK" ip daddr 224.0.0.0/4 return
fi
EOF
  chmod 755 "$FW_SCRIPT"

  # Boot-time oneshot. After docker+incus so both chains exist; Wants incus so
  # the daemon (and thus the bridge + pstrt chain) is up at boot rather than
  # only on first socket use. RemainAfterExit so it shows as active.
  log "install $FW_UNIT (reapplies the rules each boot)"
  cat > "$FW_UNIT" <<EOF
[Unit]
Description=ycluster dev bridge egress (Docker DOCKER-USER accept + VRRP NAT exempt)
After=docker.service incus.service nftables.service
Wants=incus.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$FW_SCRIPT

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable yc-dev-firewall.service
  # Apply now so the current boot is fixed without waiting for a reboot.
  systemctl restart yc-dev-firewall.service
}

# --- §2 per-user socket --------------------------------------------------
ensure_user_socket() {
  log "enable incus-user.socket / incus-user.service"
  systemctl enable --now incus-user.socket incus-user.service
}

# --- §3 group membership (restricted socket) -----------------------------
ensure_group() {
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx incus; then
    log "$TARGET_USER already in group incus"
  else
    log "add $TARGET_USER to group incus"
    usermod -aG incus "$TARGET_USER"
    log "note: $TARGET_USER must re-login (or 'newgrp incus') for it to take effect"
  fi
}

# --- §5 subuid/subgid for root (uid-shifted unprivileged containers) -----
ensure_subids() {
  local f
  for f in /etc/subuid /etc/subgid; do
    [ -f "$f" ] || { : > "$f"; }
    if grep -q "^root:" "$f"; then
      log "$f already has a root range"
    else
      log "add root:$SUBID_RANGE to $f"
      printf 'root:%s\n' "$SUBID_RANGE" >> "$f"
    fi
  done
}

# --- grant the per-user project access to the bridge ---------------------
# incus-user auto-provisions $PROJECT on the user's first connection. Trigger
# that now (as the user, via the freshly-granted group), then grant the
# restricted project access to the shared bridge it can't create itself.
ensure_project_access() {
  if ! incus project show "$PROJECT" >/dev/null 2>&1; then
    log "triggering incus-user provisioning of $PROJECT"
    # `sudo -u` picks up the just-added group; ignore output, we only want
    # the side effect of the project being created.
    sudo -u "$TARGET_USER" incus list >/dev/null 2>&1 || true
  fi
  if ! incus project show "$PROJECT" >/dev/null 2>&1; then
    log "warning: $PROJECT not present yet — it will be created when $TARGET_USER"
    log "         first runs incus; re-run this script afterwards to grant access"
    return 0
  fi
  local cur
  cur="$(incus project get "$PROJECT" restricted.networks.access 2>/dev/null || true)"
  if [ "$cur" != "$NETWORK" ]; then
    log "grant $PROJECT access to $NETWORK"
    incus project set "$PROJECT" restricted.networks.access "$NETWORK"
  else
    log "$PROJECT already has access to $NETWORK"
  fi
}

# --- teardown ------------------------------------------------------------
teardown_firewall() {
  if systemctl list-unit-files yc-dev-firewall.service >/dev/null 2>&1; then
    log "disable + remove yc-dev-firewall.service"
    systemctl disable --now yc-dev-firewall.service >/dev/null 2>&1 || true
  fi
  rm -f "$FW_UNIT" "$FW_SCRIPT"
  systemctl daemon-reload 2>/dev/null || true
  command -v nft >/dev/null 2>&1 || return 0
  # Delete the live rules we added, by handle (idempotent: no match -> no-op).
  local h
  if nft list chain ip filter DOCKER-USER >/dev/null 2>&1; then
    for h in $(nft -a list chain ip filter DOCKER-USER \
                 | grep "\"$NETWORK\"" | grep -oE 'handle [0-9]+' | awk '{print $2}'); do
      log "delete DOCKER-USER rule handle $h ($NETWORK)"
      nft delete rule ip filter DOCKER-USER handle "$h" || true
    done
  fi
  if nft list chain inet incus "pstrt.$NETWORK" >/dev/null 2>&1; then
    for h in $(nft -a list chain inet incus "pstrt.$NETWORK" \
                 | grep "224.0.0.0/4" | grep -oE 'handle [0-9]+' | awk '{print $2}'); do
      log "delete pstrt.$NETWORK multicast rule handle $h"
      nft delete rule inet incus "pstrt.$NETWORK" handle "$h" || true
    done
  fi
}

teardown_project_access() {
  if incus project show "$PROJECT" >/dev/null 2>&1 && \
     [ "$(incus project get "$PROJECT" restricted.networks.access 2>/dev/null || true)" = "$NETWORK" ]; then
    log "revoke $PROJECT access to $NETWORK"
    incus project unset "$PROJECT" restricted.networks.access || true
  fi
}

teardown_network() {
  if incus network show "$NETWORK" >/dev/null 2>&1; then
    log "delete network $NETWORK (fails harmlessly if still in use)"
    incus network delete "$NETWORK" || \
      log "warning: $NETWORK still in use (tear the dev cluster down first)"
  fi
}

teardown_group() {
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx incus; then
    log "remove $TARGET_USER from group incus"
    gpasswd -d "$TARGET_USER" incus >/dev/null || true
  fi
}

# --- run -----------------------------------------------------------------
if [ "$MODE" = teardown ]; then
  log "teardown for $TARGET_USER (uid $TARGET_UID)"
  teardown_firewall
  teardown_project_access
  teardown_network
  teardown_group
  log "done. left in place: incus install, daemon, '$POOL' pool, /etc/subuid+subgid."
  exit 0
fi

ensure_installed
ensure_daemon
ensure_pool
ensure_network
ensure_firewall
ensure_user_socket
ensure_group
ensure_subids
ensure_project_access

log "done. verify as the user:  incus launch images:debian/13 c1 && incus list"
