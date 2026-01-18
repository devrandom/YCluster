#!/bin/bash
# dnsmasq dhcp-script - registers nodes in etcd via admin API
# Called by dnsmasq on DHCP lease events
# Arguments: $1=action (add/del/old), $2=MAC, $3=IP, $4=hostname (optional)
#
# NOTE: This script runs as ROOT (called by dnsmasq which runs as root).
# Cannot use rootless Docker commands here - use HTTP APIs instead.

ACTION="$1"
MAC="$2"
IP="$3"
HOSTNAME="${4:-}"

ADMIN_API="http://localhost:12723"
LOG_TAG="dhcp-script"

log() {
    logger -t "$LOG_TAG" "$@"
}

case "$ACTION" in
    add|old)
        # Register node on new or renewed lease
        log "Registering node: MAC=$MAC IP=$IP HOSTNAME=$HOSTNAME"

        # Determine node type from hostname prefix if available
        TYPE_PARAM=""
        if [[ -n "$HOSTNAME" ]]; then
            case "${HOSTNAME:0:1}" in
                s) TYPE_PARAM="&type=storage" ;;
                c) TYPE_PARAM="&type=compute" ;;
                m) TYPE_PARAM="&type=macos" ;;
            esac
        fi

        # Call admin API to register
        RESPONSE=$(curl -sf "${ADMIN_API}/api/allocate?mac=${MAC}${TYPE_PARAM}" 2>&1)
        if [[ $? -eq 0 ]]; then
            log "Registration successful: $RESPONSE"
            # Regenerate dhcp-hosts file so next DHCP request gets correct IP
            SCRIPT_DIR="$(dirname "$0")"
            "${SCRIPT_DIR}/generate-dhcp-hosts.sh" 2>&1 | while read -r line; do log "$line"; done
        else
            log "Registration failed: $RESPONSE"
        fi
        ;;
    del)
        log "Lease deleted: MAC=$MAC IP=$IP"
        # Don't remove from etcd - node might come back
        ;;
    *)
        log "Unknown action: $ACTION"
        ;;
esac

exit 0
