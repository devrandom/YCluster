#!/bin/bash
# Auto-restart dnsmasq on file changes
#
# NOTE: This script must run as ROOT (dnsmasq needs root for DHCP port 67).
# Run with: sudo ./dev/dnsmasq-watch.sh
# Uses SUDO_USER to run make as the original user for file ownership.

set -e

cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must run as root"
    exit 1
fi

# Check for inotifywait
if ! command -v inotifywait &>/dev/null; then
    echo "Installing inotify-tools..."
    apt-get install -y inotify-tools
fi

# Files to watch
WATCH_FILES=(
    dev/dnsmasq.conf.template
    dev/grub.cfg
)

# Generate initial config
echo "Generating initial config..."
sudo -u "${SUDO_USER:-dev}" make ./dev/dnsmasq.conf

cleanup() {
    echo "Stopping dnsmasq..."
    pkill -f "dnsmasq -d.*dev/dnsmasq.conf" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

start_dnsmasq() {
    pkill -f "dnsmasq -d.*dev/dnsmasq.conf" 2>/dev/null || true
    sleep 0.5
    echo "Starting dnsmasq..."
    dnsmasq -d --log-debug -C "$(pwd)/dev/dnsmasq.conf" --user=dev --group=dev &
    DNSMASQ_PID=$!
    echo "dnsmasq running (PID $DNSMASQ_PID)"
}

start_dnsmasq

echo "Watching for changes: ${WATCH_FILES[*]}"
echo "Press Ctrl-C to stop"

while true; do
    inotifywait -q -e modify,create "${WATCH_FILES[@]}" 2>/dev/null && {
        echo ""
        echo "=== File changed, restarting... ==="
        sudo -u "${SUDO_USER:-dev}" make ./dev/dnsmasq.conf ./dev/tftpboot/grub/grub.cfg
        start_dnsmasq
    }
done
