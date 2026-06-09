#!/bin/bash
# Sync local repo to s3.yc:/opt/infrastructure/ using rsync + watchman.
# Runs an initial sync, then re-syncs on any file change.
#
# With --once, do a single sync and exit (no watchman, no watch) — handy when
# the watch connection is flaky and you just want one clean push. The exit
# status reflects rsync's.
#
# With --stop, stop a running watch (from any terminal) and remove the watchman
# trigger + watch for this repo. It kills the watcher's tail so the watcher
# exits cleanly, then tears down the trigger/watch.
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
    if rsync "${rsync_opts[@]}" "$REPO/" "$DEST"; then
        echo "[$(date +%T)] done"
    else
        local rc=$?
        echo "[$(date +%T)] rsync failed (rc=$rc)"
        return "$rc"
    fi
}

if [[ "${1:-}" == "--once" ]]; then
    do_sync
    exit
fi

if [[ "${1:-}" == "--stop" ]]; then
    # A running watcher sits in `wait` on `tail -f $LOG`; killing that tail lets
    # it fall through to its own cleanup (trigger-del) and exit. This works even
    # for an instance started before --stop existed. Then belt-and-suspenders:
    # remove the trigger + watch directly in case no watcher was running.
    if pkill -f "tail -f $LOG"; then
        echo "stopped running dev-sync watcher"
    fi
    watchman trigger-del "$REPO" dev-sync >/dev/null 2>&1 || true
    watchman watch-del "$REPO" >/dev/null 2>&1 || true
    echo "removed watchman trigger + watch for $REPO"
    exit
fi

cleanup() {
    kill "$TAIL_PID" 2>/dev/null || true
    watchman trigger-del "$REPO" dev-sync >/dev/null 2>&1 || true
    echo "stopped"
}
trap cleanup EXIT INT TERM

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

do_sync || true
echo "watching $REPO — background log: $LOG (Ctrl-C to stop)"
# Truncate so tail -f shows only this session's output. The watchman trigger
# appends here and persists across runs, so an un-truncated log replays stale
# failures from previous sessions on every startup.
: > "$LOG"
tail -f "$LOG" &
TAIL_PID=$!
wait $TAIL_PID
