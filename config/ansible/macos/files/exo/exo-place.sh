#!/usr/bin/env bash
# Place the Kimi-K2.6 instance on the exo cluster.
#
# exo does NOT persist instance placements across a restart, so the
# instance has to be (re)placed every time exo cycles. Two modes:
#
#   ./exo-place.sh              one-shot: place once, wait for RunnerReady
#   SUPERVISE=1 ./exo-place.sh  supervisor loop: keep the instance placed,
#                               re-placing whenever exo comes up with none
#
# Supervisor mode is what the com.ycluster.exo-place LaunchDaemon runs
# on every exo mac so K2.6 auto-recovers after any reboot or exo
# restart. The supervisors self-elect a single placement leader (see
# is_placement_leader) so only one of them ever places. One-shot mode is
# for ad-hoc manual re-placement.
#
# Run it on a mac (uses localhost) or from anywhere with cluster access:
#   ./exo-place.sh                 # on a mac running exo
#   EXO_HOST=m1.yc ./exo-place.sh  # from elsewhere
#
# Override model / topology via env:
#   MODEL_ID=... SHARDING=Pipeline INSTANCE_META=MlxRing ./exo-place.sh

set -euo pipefail

: "${EXO_HOST:=localhost}"
: "${EXO_PORT:=52415}"
: "${MODEL_ID:=mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8}"
: "${SHARDING:=Tensor}"
: "${INSTANCE_META:=MlxJaccl}"
: "${MIN_NODES:=2}"
: "${SUPERVISE:=0}"      # 1 = run as a re-placement supervisor loop
: "${POLL_INTERVAL:=30}" # supervisor poll cadence, seconds
: "${WAIT:=1}"           # one-shot: 0 to skip the readiness poll

api="http://${EXO_HOST}:${EXO_PORT}"

log() { echo "$(date '+%F %T')  $*"; }

# Echo each runner's state, one per line. Empty output means exo has no
# runners, or exo is unreachable — the caller distinguishes via api_up.
runner_states() {
    local state
    state=$(curl -s -m 8 "${api}/state" 2>/dev/null) || return 0
    printf '%s' "$state" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin).get('runners', {})
except Exception:
    sys.exit(0)
for v in r.values():
    print(list(v)[0] if isinstance(v, dict) else str(v))
" 2>/dev/null || true
}

api_up() { curl -s -m 5 -o /dev/null "${api}/state" 2>/dev/null; }

# True if this node is the placement leader: the lowest-sorted node_id
# among the currently-connected cluster (topology.nodes). exo exposes no
# elected-master API, so the supervisors self-elect deterministically —
# a single actor that follows liveness (if the leader drops out, the
# next-lowest connected node takes over). Returns non-zero if the local
# node id or topology can't be read.
is_placement_leader() {
    local me min
    me=$(curl -s -m 5 "${api}/node_id" 2>/dev/null | tr -d '"[:space:]')
    [[ -n "$me" ]] || return 1
    min=$(curl -s -m 8 "${api}/state" 2>/dev/null | python3 -c "
import sys, json
try:
    nodes = json.load(sys.stdin).get('topology', {}).get('nodes', [])
except Exception:
    sys.exit(0)
if nodes:
    print(sorted(nodes)[0])
" 2>/dev/null)
    [[ -n "$min" && "$me" == "$min" ]]
}

# Echo the number of placed instances exo currently holds. An instance
# appears as soon as place_instance returns — well before its runners
# spawn — so this is the correct "is something placed?" signal for the
# supervisor (runners stay empty for a while during load).
instance_count() {
    curl -s -m 8 "${api}/state" 2>/dev/null | python3 -c "
import sys, json
try:
    print(len(json.load(sys.stdin).get('instances', {})))
except Exception:
    print(0)
" 2>/dev/null || echo 0
}

place() {
    log "placing ${MODEL_ID} (sharding=${SHARDING} meta=${INSTANCE_META} min_nodes=${MIN_NODES}) via ${api}"
    curl -sS -m 30 -X POST "${api}/place_instance" \
        -H "Content-Type: application/json" \
        -d "{\"model_id\":\"${MODEL_ID}\",\"sharding\":\"${SHARDING}\",\"instance_meta\":\"${INSTANCE_META}\",\"min_nodes\":${MIN_NODES}}"
    echo
}

# Poll runner states until every runner reports RunnerReady. Returns
# non-zero on an Error/Failed/Crash state or after the deadline.
wait_ready() {
    local prev="" cur deadline
    deadline=$(( $(date +%s) + 1800 ))
    while (( $(date +%s) < deadline )); do
        cur=$(runner_states | sort | tr '\n' ' ')
        cur=${cur% }
        if [[ "$cur" != "$prev" ]]; then
            log "  runners: ${cur:-none yet}"
            prev="$cur"
        fi
        case "$cur" in
            *Error*|*Failed*|*Crash*) echo "Placement failed: $cur" >&2; return 1 ;;
        esac
        if [[ -n "$cur" && "$cur" != *RunnerLoading* && "$cur" != *RunnerConnected* \
              && "$cur" != *WarmingUp* && "$cur" == *RunnerReady* ]]; then
            log "K2.6 is up."
            return 0
        fi
        sleep 20
    done
    echo "Timed out waiting for RunnerReady." >&2
    return 1
}

if [[ "${SUPERVISE}" == "1" ]]; then
    log "exo placement supervisor started for ${MODEL_ID} via ${api}"
    was_leader=""
    while true; do
        if api_up; then
            if is_placement_leader; then
                if [[ "${was_leader}" != "1" ]]; then
                    log "this node is the placement leader"
                    was_leader=1
                fi
                # Only (re)place when exo holds no instance. An instance
                # is registered the moment place_instance returns, so
                # this never double-places while a placement is still
                # loading. exo drops all instances on restart.
                if [[ "$(instance_count)" == "0" ]]; then
                    place || log "place_instance failed (will retry); cluster may not have ${MIN_NODES} nodes yet"
                fi
            elif [[ "${was_leader}" != "0" ]]; then
                log "another node is the placement leader; standing by"
                was_leader=0
            fi
        fi
        sleep "${POLL_INTERVAL}"
    done
fi

# One-shot mode.
place
echo
if [[ "${WAIT}" != "1" ]]; then
    echo "Skipping readiness poll (WAIT=0). Check with:"
    echo "  curl -s ${api}/state | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"runners\"])'"
    exit 0
fi
log "Waiting for runners to reach RunnerReady (large load, a few minutes)..."
wait_ready
