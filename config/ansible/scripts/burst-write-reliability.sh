#!/bin/bash
# burst-write-reliability.sh — bursty-write reliability probe for a storage device.
#
# Each cycle writes BURST_SIZE of data with continuous byte-exact pattern
# verify, then sleeps IDLE_DURATION before the next cycle. fio writes and
# verifies the full BURST_SIZE without a runtime cap, so the verify phase
# always runs (no "verification read phase will never start" warning).
#
# By default writes are uncapped so the drive runs at its natural speed;
# set BURST_RATE to throttle (e.g. BURST_RATE=70M).
#
# Default: ~26 cycles of 50 GiB at full speed (~3 min write + ~3 min verify)
# + 50 min idle = ~1.3 TiB over 24 h.
#
# Tunable via env:
#   TOTAL_DURATION (default 86400) — total wall-clock seconds
#   BURST_SIZE     (default 50G)   — bytes written per cycle (accepts K/M/G)
#   IDLE_DURATION  (default 3000)  — sleep between cycles
#   BURST_RATE     (default unset) — optional fio --rate cap (e.g. 70M)
#
# Usage: burst-write-reliability.sh DEVICE MOUNT

set -euo pipefail

DEV="${1:?usage: $0 DEVICE MOUNT}"
MOUNT="${2:?usage: $0 DEVICE MOUNT}"

TOTAL_DURATION="${TOTAL_DURATION:-86400}"
BURST_SIZE="${BURST_SIZE:-50G}"
IDLE_DURATION="${IDLE_DURATION:-3000}"
BURST_RATE="${BURST_RATE:-}"

LOG="/var/log/burst-write-reliability-$(date +%Y%m%d-%H%M%S).log"
SMART_LOG="/tmp/smart.log"
SMART_INTERVAL=3600
SMART_DEV_FLAG=""

# Estimate per-cycle duration for logging.
BURST_SIZE_GIB="${BURST_SIZE%G}"
BURST_SIZE_MB=$(( BURST_SIZE_GIB * 1024 ))
EST_RATE_MB=300   # default write estimate when uncapped
if [[ -n "$BURST_RATE" ]]; then
  EST_RATE_MB="${BURST_RATE%M}"
fi
WRITE_SEC=$(( BURST_SIZE_MB / EST_RATE_MB ))
VERIFY_SEC=$(( BURST_SIZE_MB / 300 ))   # ~300 MB/s read estimate (cycle timing)
CYCLE_SEC=$(( WRITE_SEC + VERIFY_SEC + IDLE_DURATION ))
CYCLES=$(( TOTAL_DURATION / CYCLE_SEC ))

# Optional --rate flag (only added when BURST_RATE is set).
RATE_FLAG=""
if [[ -n "$BURST_RATE" ]]; then
  RATE_FLAG="--rate=$BURST_RATE"
fi

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG"
}

# smartctl needs a device type for USB-NVMe bridges (e.g. sntasmedia); auto-
# detect misses some of them, so probe the common USB passthrough types.
pick_smart_dev() {
  local dt
  if smartctl -a "$DEV" >/dev/null 2>&1; then
    SMART_DEV_FLAG=""
    return 0
  fi
  for dt in sntasmedia sntjmicron sntrealtek sat; do
    if smartctl -a -d "$dt" "$DEV" >/dev/null 2>&1; then
      SMART_DEV_FLAG="-d $dt"
      return 0
    fi
  done
  SMART_DEV_FLAG=""
  return 1
}

cleanup() {
  if [[ -n "${WATCH_PID:-}"  ]]; then kill "${WATCH_PID}"  2>/dev/null || true; fi
  if [[ -n "${DMESG_PID:-}"  ]]; then kill "${DMESG_PID}"  2>/dev/null || true; fi
  if [[ -n "${SMART_PID:-}"  ]]; then kill "${SMART_PID}"  2>/dev/null || true; fi
  if [[ -n "${FIO_PID:-}"    ]]; then kill "${FIO_PID}"    2>/dev/null || true; fi
  wait 2>/dev/null || true
  rm "$MOUNT"/.bwr-cycle-*-* "$MOUNT"/.bwr-cycle-*-*.verify 2>/dev/null || true
  log "cleanup done; test ended"
}
trap cleanup EXIT INT TERM

log "burst-write reliability test starting"
log "device:         $DEV"
log "mount:          $MOUNT"
log "duration:       ${TOTAL_DURATION}s ($((TOTAL_DURATION/3600))h)"
log "burst size:     ${BURST_SIZE} per cycle (write + verify)"
log "idle:           ${IDLE_DURATION}s between cycles"
if [[ -n "$BURST_RATE" ]]; then
  log "burst rate:     $BURST_RATE (capped)"
else
  log "burst rate:     unlimited"
fi
log "approx cycle:   ${WRITE_SEC}s write + ${VERIFY_SEC}s verify + ${IDLE_DURATION}s idle = ${CYCLE_SEC}s"
log "approx cycles:  $CYCLES"
log "approx total:   ~$(( CYCLES * BURST_SIZE_MB / 1024 / 1024 )) TiB"
log "log file:       $LOG"
log "smart log:      $SMART_LOG"
log ""
log "--- initial smartctl (output -> $SMART_LOG) ---"
: > "$SMART_LOG"
if pick_smart_dev; then
  log "smartctl device type:${SMART_DEV_FLAG:- auto}"
else
  log "smartctl: no working device type found; SMART reporting disabled"
fi
smartctl -a $SMART_DEV_FLAG "$DEV" >> "$SMART_LOG" 2>&1 || log "smartctl failed"
log ""

udevadm monitor --subsystem-match=block --property >> "$LOG" 2>&1 &
WATCH_PID=$!
log "started udevadm monitor (pid $WATCH_PID)"

( dmesg -w 2>/dev/null | grep --line-buffered -iE "$DEV|error|reset" ) >> "$LOG" 2>&1 &
DMESG_PID=$!
log "started dmesg watcher (pid $DMESG_PID)"

(
  while sleep "$SMART_INTERVAL"; do
    {
      echo "--- smartctl $(date -Iseconds) ---"
      smartctl -a $SMART_DEV_FLAG "$DEV" 2>&1 || echo "(smartctl failed)"
    } >> "$SMART_LOG"
  done
) &
SMART_PID=$!
log "started smartctl poller (pid $SMART_PID, every ${SMART_INTERVAL}s -> $SMART_LOG)"

START=$(date +%s)
CYCLE=0
while :; do
  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED >= TOTAL_DURATION )); then break; fi
  CYCLE=$((CYCLE+1))
  FIO_FILE="$MOUNT/.bwr-cycle-${CYCLE}-$$"

  log "cycle $CYCLE: starting burst (write ${BURST_SIZE}${RATE_FLAG:+ at $BURST_RATE}) -> $FIO_FILE"
  fio --name=burst-write-rel \
      --filename="$FIO_FILE" \
      --size="$BURST_SIZE" \
      --bs=1M \
      --rw=write \
      --ioengine=libaio \
      --direct=1 \
      $RATE_FLAG \
      --verify=pattern \
      --verify_pattern=0x5a5aa5a5 \
      --verify_state_save=1 \
      2>&1 | tee -a "$LOG"
  log "cycle $CYCLE: burst complete (write + verify)"

  rm "$FIO_FILE" 2>/dev/null || true; rm "${FIO_FILE}.verify" 2>/dev/null || true
  log "cycle $CYCLE: test file removed"

  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED >= TOTAL_DURATION )); then break; fi
  REMAINING=$(( TOTAL_DURATION - ELAPSED ))
  THIS_IDLE=$(( IDLE_DURATION < REMAINING ? IDLE_DURATION : REMAINING ))

  log "cycle $CYCLE: idling for ${THIS_IDLE}s"
  sleep "$THIS_IDLE"
  log "cycle $CYCLE: idle ended"
done

log ""
log "--- final smartctl (output -> $SMART_LOG) ---"
smartctl -a $SMART_DEV_FLAG "$DEV" >> "$SMART_LOG" 2>&1 || log "smartctl failed"
log ""
log "completed $CYCLE cycles"
