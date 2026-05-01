#!/bin/bash
# Sync local repo to s3.yc:/opt/infrastructure/ using rsync + watchman.
# Runs an initial sync, then re-syncs on any file change.

set -euo pipefail

DEST="s3.yc:/opt/infrastructure/"
REPO="$(cd "$(dirname "$0")" && pwd)"

rsync_opts=(-az --exclude-from="$REPO/.watchignore")

do_sync() {
    echo "[$(date +%T)] syncing..."
    rsync "${rsync_opts[@]}" "$REPO/" "$DEST" && echo "[$(date +%T)] done" || echo "[$(date +%T)] rsync failed"
}

cleanup() {
    watchman watch-del "$REPO" >/dev/null 2>&1 || true
    echo "stopped"
}
trap cleanup EXIT

watchman watch "$REPO" >/dev/null
watchman -- trigger "$REPO" dev-sync '*' -- bash -c "
    rsync -az --exclude-from='$REPO/.watchignore' '$REPO/' '$DEST' \
        && echo \"[\$(date +%T)] done\" \
        || echo \"[\$(date +%T)] rsync failed\"
" >/dev/null

do_sync
echo "watching $REPO (Ctrl-C to stop)"
while true; do sleep 1; done
