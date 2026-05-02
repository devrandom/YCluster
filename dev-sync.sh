#!/bin/bash
# Sync local repo to s3.yc:/opt/infrastructure/ using rsync + watchman.
# Runs an initial sync, then re-syncs on any file change.
#
# The watchman trigger persists in the watchman daemon across restarts of
# this script. Ctrl-C only removes the trigger (not the watch root).

set -euo pipefail

DEST="s3.yc:/opt/infrastructure/"
REPO="$(cd "$(dirname "$0")" && pwd)"
LOG="/tmp/dev-sync.log"

rsync_opts=(-az --exclude-from="$REPO/.watchignore")

do_sync() {
    echo "[$(date +%T)] syncing..."
    rsync "${rsync_opts[@]}" "$REPO/" "$DEST" && echo "[$(date +%T)] done" || echo "[$(date +%T)] rsync failed"
}

cleanup() {
    kill "$TAIL_PID" 2>/dev/null || true
    watchman trigger-del "$REPO" dev-sync >/dev/null 2>&1 || true
    echo "stopped"
}
trap cleanup EXIT

watchman watch "$REPO" >/dev/null

# Use JSON trigger spec — the CLI `-- trigger` shorthand has issues with
# watchman 4.9 (stdout/stderr redirect fields not supported, * glob
# only matches root-level files). The JSON spec is explicit and reliable.
watchman -j <<EOF >/dev/null
["trigger", "$REPO", {
  "name": "dev-sync",
  "expression": ["allof", ["type", "f"], ["not", ["anyof",
    ["match", ".git/**", "wholename"],
    ["match", "**/__pycache__/**", "wholename"],
    ["match", "**/.venv/**", "wholename"],
    ["match", "**/venv/**", "wholename"]
  ]]],
  "command": ["bash", "-c", "rsync -az --exclude-from='$REPO/.watchignore' '$REPO/' '$DEST' >> $LOG 2>&1 && echo \"[\$(date +%T)] done\" >> $LOG || echo \"[\$(date +%T)] rsync failed\" >> $LOG"],
  "stdin": "/dev/null"
}]
EOF

do_sync
echo "watching $REPO — background log: $LOG (Ctrl-C to stop)"
touch "$LOG"
tail -f "$LOG" &
TAIL_PID=$!
wait $TAIL_PID
