#!/usr/bin/env bash
# Place the Kimi-K2.6 instance on the exo cluster.
#
# exo does NOT persist instance placements across a restart — every time
# exo is cycled (run-exo.sh) the instance has to be placed again. This
# script holds the placement stanza so that's a one-liner.
#
# Run it on a mac (uses localhost) or from anywhere with cluster access:
#   ./exo-place.sh                 # on a mac running exo
#   EXO_HOST=m1.yc ./exo-place.sh  # from elsewhere
#
# Override the model / topology via env if needed:
#   MODEL_ID=... SHARDING=Pipeline INSTANCE_META=MlxRing ./exo-place.sh

set -euo pipefail

: "${EXO_HOST:=localhost}"
: "${EXO_PORT:=52415}"
: "${MODEL_ID:=mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8}"
: "${SHARDING:=Tensor}"
: "${INSTANCE_META:=MlxJaccl}"
: "${MIN_NODES:=2}"
: "${WAIT:=1}"  # set WAIT=0 to skip the readiness poll

api="http://${EXO_HOST}:${EXO_PORT}"

echo "Placing ${MODEL_ID}"
echo "  sharding=${SHARDING} instance_meta=${INSTANCE_META} min_nodes=${MIN_NODES}"
echo "  via ${api}"
echo

curl -sS -m 30 -X POST "${api}/place_instance" \
    -H "Content-Type: application/json" \
    -d "{\"model_id\":\"${MODEL_ID}\",\"sharding\":\"${SHARDING}\",\"instance_meta\":\"${INSTANCE_META}\",\"min_nodes\":${MIN_NODES}}"
echo
echo

if [[ "${WAIT}" != "1" ]]; then
    echo "Skipping readiness poll (WAIT=0). Check with:"
    echo "  curl -s ${api}/state | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"runners\"])'"
    exit 0
fi

echo "Waiting for runners to reach RunnerReady (438 GB load, a few minutes)..."
prev=""
deadline=$(( $(date +%s) + 1800 ))
while (( $(date +%s) < deadline )); do
    state=$(curl -s -m 8 "${api}/state" 2>/dev/null) || { sleep 15; continue; }
    cur=$(printf '%s' "$state" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin).get('runners', {})
except Exception:
    print('?'); sys.exit()
if not r:
    print('none yet')
else:
    print(' '.join(sorted(list(v)[0] if isinstance(v, dict) else str(v) for v in r.values())))
")
    if [[ "${cur}" != "${prev}" ]]; then
        echo "  $(date +%H:%M:%S)  ${cur}"
        prev="${cur}"
    fi
    case "${cur}" in
        *Error*|*Failed*|*Crash*) echo "Placement failed: ${cur}" >&2; exit 1 ;;
    esac
    # all runners ready (and at least one present)
    if [[ -n "${cur}" && "${cur}" != *"none yet"* && "${cur}" != *RunnerLoading* \
          && "${cur}" != *RunnerConnected* && "${cur}" == *RunnerReady* ]]; then
        echo "K2.6 is up."
        exit 0
    fi
    sleep 20
done
echo "Timed out waiting for RunnerReady." >&2
exit 1
