#!/bin/bash
# Generate dnsmasq dhcp-hostsfile from etcd allocations
# Format: mac,ip,hostname
#
# NOTE: This script may run as ROOT (when called by dhcp-script.sh via dnsmasq)
# or as regular user (when run manually). Uses etcd HTTP API directly to avoid
# rootless Docker issues.

SCRIPT_DIR="$(dirname "$0")"
HOSTSFILE="${1:-${SCRIPT_DIR}/dhcp-hosts}"
ETCD_ENDPOINT="${ETCD_ENDPOINT:-http://localhost:2379}"

# Function to format MAC with colons (aabbccddeeff -> aa:bb:cc:dd:ee:ff)
format_mac() {
    echo "$1" | sed 's/\(..\)/\1:/g; s/:$//'
}

# Get all allocations from etcd via HTTP API and format for dnsmasq
# etcd v3 HTTP API uses base64 encoding
PREFIX_B64=$(echo -n "/cluster/nodes/by-hostname/" | base64 -w0)
RANGE_END_B64=$(echo -n "/cluster/nodes/by-hostname0" | base64 -w0)  # '0' is after '/' in ASCII

curl -sf "${ETCD_ENDPOINT}/v3/kv/range" \
    -X POST \
    -d "{\"key\": \"${PREFIX_B64}\", \"range_end\": \"${RANGE_END_B64}\"}" 2>/dev/null | \
    jq -r '.kvs[]?.value // empty | @base64d | fromjson | select(.mac and .ip and .hostname) | "\(.mac),\(.ip),\(.hostname)"' | \
    while IFS=, read -r mac ip hostname; do
        echo "$(format_mac "$mac"),$ip,$hostname"
    done > "${HOSTSFILE}.tmp"

# Only update if changed
if ! cmp -s "${HOSTSFILE}.tmp" "${HOSTSFILE}" 2>/dev/null; then
    mv "${HOSTSFILE}.tmp" "${HOSTSFILE}"
    echo "Updated ${HOSTSFILE}"
    # Signal dnsmasq to reload if running
    pkill -SIGHUP -f "dnsmasq.*dev/dnsmasq.conf" 2>/dev/null && echo "Signaled dnsmasq to reload" || true
else
    rm -f "${HOSTSFILE}.tmp"
    echo "No changes"
fi
