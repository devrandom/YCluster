#!/bin/sh
# Un-bridge the active Thunderbolt interface and give it a static IP so
# exo's JACCL/RDMA backend can bring up queue pairs. RDMA fails when the
# TB port is a PROMISC member of bridge0 ("Changing queue pair to RTR
# failed with errno 22"), so the port must be standalone.
#
# Installed as /usr/local/ycluster/tb-rdma-setup.sh and run at boot from
# /Library/LaunchDaemons/com.ycluster.tb-rdma.plist.
#
# Boot-time race: macOS configd auto-bundles TB ports into bridge0
# *asynchronously* after boot. A naive un-bridge at RunAtLoad can run
# before bridge0 exists, delete a non-member (no-op), and then configd
# bridges the port anyway. So this script (1) WAITS for the TB port to
# be enumerated and bridged before acting, and (2) runs a short guard
# loop afterwards, re-applying if configd re-bridges the port.
#
# Usage: tb-rdma-setup.sh <static-ip>

set -u
IP="${1:?usage: tb-rdma-setup.sh <static-ip>}"
NETMASK=255.255.255.0
LOG=/var/log/tb-rdma.log

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') tb-rdma: $*" >> "$LOG"; }

# Locate the active Thunderbolt interface. Two cases:
#  - boot: the port is still an active member of bridge0
#  - re-run: we already un-bridged it, so match by the static IP instead
detect_iface() {
    cfg=$(ifconfig 2>/dev/null | awk -v ip="$IP" \
        '/^[a-z]/{i=$1} $1=="inet" && $2==ip {sub(/:$/,"",i); print i; exit}')
    if [ -n "$cfg" ]; then
        echo "$cfg"
        return 0
    fi
    for m in $(ifconfig bridge0 2>/dev/null | awk '/member:/{print $2}'); do
        if ifconfig "$m" 2>/dev/null | grep -q "status: active"; then
            echo "$m"
            return 0
        fi
    done
    return 1
}

apply() {
    ifconfig bridge0 deletem "$1" 2>/dev/null
    ifconfig "$1" inet "$IP" netmask "$NETMASK" up
}

log "starting, target IP $IP"

# Wait up to 180s for an active TB interface to appear.
IFACE=""
i=0
while [ "$i" -lt 180 ]; do
    IFACE=$(detect_iface) && break
    i=$((i + 1))
    sleep 1
done

if [ -z "$IFACE" ]; then
    log "no active Thunderbolt interface after 180s (TB link down?) — giving up"
    exit 0
fi
log "active Thunderbolt interface: $IFACE"

apply "$IFACE"
log "configured: $(ifconfig "$IFACE" 2>/dev/null | tr '\n' ' ')"

# Guard window: configd may re-bridge the port for a short time after
# boot. Re-apply if that happens.
i=0
while [ "$i" -lt 15 ]; do
    sleep 2
    if ifconfig bridge0 2>/dev/null | grep -q "member: $IFACE"; then
        log "$IFACE was re-bridged by configd — re-applying"
        apply "$IFACE"
    fi
    i=$((i + 1))
done

log "done: $(ifconfig "$IFACE" 2>/dev/null | grep -E 'inet |status' | tr '\n' ' ')"
