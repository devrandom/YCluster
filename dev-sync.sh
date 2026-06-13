#!/bin/bash
# Sync local repo to s3.yc:/opt/infrastructure/ using rsync + watchman.
#
# Commands (run with no args for usage):
#   --start  Set up the watchman trigger + run an initial sync, then return.
#            The watcher keeps running in the watchman daemon after this script
#            exits — that's what makes --start able to background cleanly.
#   --stop   Remove the watchman trigger + watch for this repo (stops auto-sync).
#   --check  Dry-run checksum comparison against the cluster: print any files
#            whose content differs (no transfer, no deletes) and report whether
#            the watcher is running. Empty diff + exit 0 means in sync; exit 1
#            if anything differs. Same exclude set as the live sync, so it's the
#            authoritative "did my edits land?" check.
#   --once   Do a single sync and exit (no watchman, no watch) — handy when the
#            watch connection is flaky and you just want one clean push. Exit
#            status reflects rsync's.
#
# The watchman trigger persists in the watchman daemon across restarts of this
# script and is what actually re-syncs on edits, independently of any foreground
# process. --stop tears it down.

set -euo pipefail

DEST="s3.yc:/opt/infrastructure/"
REPO="$(cd "$(dirname "$0")" && pwd)"
LOG="/tmp/dev-sync.log"

rsync_opts=(-az --exclude-from="$REPO/.watchignore")

usage() {
    cat <<EOF
Usage: $(basename "$0") <command>

Sync local repo to $DEST using rsync + watchman.

Commands:
  --start   Set up the watchman trigger and run an initial sync, then return.
            The watcher keeps running via the watchman daemon (survives exit).
  --stop    Remove the watchman trigger + watch for this repo (stops auto-sync).
  --check   Dry-run checksum compare against the cluster: list files whose
            content differs (exit 1 if any), and report if the watcher is up.
  --once    Do a single sync and exit (no watcher).
EOF
}

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

do_check() {
    # Dry-run, checksum-based (-c) comparison: report files whose content
    # differs from what's deployed, using the same exclude set as the live
    # sync. No --delete — the synced set includes untracked scratch dirs that
    # legitimately differ from a clean checkout, and we only care that local
    # state landed. Exit 1 on any difference so the result is scriptable.
    local out rc
    out=$(rsync -naci --exclude-from="$REPO/.watchignore" "$REPO/" "$DEST") || {
        rc=$?
        echo "[$(date +%T)] rsync check failed (rc=$rc)" >&2
        return "$rc"
    }
    if [[ -z "$out" ]]; then
        echo "in sync with $DEST"
    else
        echo "$out"
        echo "--- out of sync: $(printf '%s\n' "$out" | wc -l) item(s) differ ---"
        return 1
    fi
}

is_running() {
    # The watchman trigger persists in the daemon and is what actually syncs on
    # edits, so its presence is the authoritative "is the watcher running"
    # signal — independent of whether any foreground process is attached.
    watchman trigger-list "$REPO" 2>/dev/null | grep -q '"dev-sync"'
}

setup_trigger() {
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
}

case "${1:-}" in
    --check)
        if is_running; then
            echo "watcher: running"
        else
            echo "watcher: NOT running — edits are not being auto-synced"
        fi
        do_check
        ;;
    --once)
        do_sync
        ;;
    --start)
        if is_running; then
            echo "watcher already running for $REPO (log: $LOG)"
            exit 0
        fi
        # Truncate so the log holds only this watcher's output; the trigger
        # appends here and persists across runs, otherwise stale failures from
        # previous sessions replay on every tail.
        : > "$LOG"
        setup_trigger
        do_sync || true
        echo "started — watching $REPO in the background; log: $LOG"
        echo "stop with: $(basename "$0") --stop"
        ;;
    --stop)
        # Kill any legacy foreground watcher (it sits in `wait` on `tail -f
        # $LOG`; killing the tail lets it fall through to its own cleanup), then
        # remove the trigger + watch directly — covers a background --start too.
        if pkill -f "tail -f $LOG"; then
            echo "stopped running dev-sync watcher"
        fi
        watchman trigger-del "$REPO" dev-sync >/dev/null 2>&1 || true
        watchman watch-del "$REPO" >/dev/null 2>&1 || true
        echo "removed watchman trigger + watch for $REPO"
        ;;
    -h|--help|"")
        usage
        ;;
    *)
        echo "unknown option: $1" >&2
        usage >&2
        exit 2
        ;;
esac
