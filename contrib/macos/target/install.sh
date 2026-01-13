#!/bin/bash
# Installation helper for Apple Silicon Macs

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE=$(ls "$SCRIPT_DIR"/*.pkg 2>/dev/null | head -1 || true)

cat <<EOF
=== macOS Installation (Apple Silicon) ===

Use the GUI installer:
1. Close this Terminal
2. Select "Install macOS" from the Recovery menu
3. Follow the prompts to select your disk
4. The bootstrap package will be installed automatically

EOF

if [[ -n "$PACKAGE" ]]; then
    echo "Bootstrap package: $(basename "$PACKAGE")"
else
    echo "Warning: No bootstrap package found on this volume."
fi
echo
