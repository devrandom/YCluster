#!/usr/bin/env bash
# Manual exo launcher for macOS cluster nodes (m1+). Run under `dev` on
# the mac itself — not on the admin host. Foreground invocation so you
# can watch the startup banner and cluster discovery; wrap in tmux/
# screen/nohup if you want to detach. LaunchDaemon comes later.
#
# Prerequisites on the mac:
#   - exo checked out at EXO_REPO (default ~/exo) on the same commit as
#     the other nodes
#   - uv installed (brew install uv)
#   - dashboard/build/ populated (build on admin host with
#     ext/build-exo-dashboard.sh and rsync the build/ dir over)
#   - models available under EXO_MODELS_DIRS
#
# The OpenAI-compatible API will listen on :52415 once startup finishes.

set -euo pipefail

: "${EXO_REPO:=$HOME/exo}"
: "${EXO_MODELS_DIRS:=$HOME/models}"
# Keep our peers off whatever random libp2p discovery may surface —
# nodes only form a cluster with others using the same namespace.
: "${EXO_LIBP2P_NAMESPACE:=ycluster}"
# Disable outbound model fetches at runtime; everything is pre-staged.
: "${EXO_OFFLINE:=1}"

if [[ ! -d "$EXO_REPO" ]]; then
    echo "Error: exo checkout not found at $EXO_REPO" >&2
    echo "  git clone https://github.com/exo-explore/exo.git $EXO_REPO" >&2
    exit 1
fi

if [[ ! -f "$EXO_REPO/dashboard/build/index.html" ]]; then
    echo "Error: dashboard build missing at $EXO_REPO/dashboard/build/" >&2
    echo "  Build on admin host with ext/build-exo-dashboard.sh and" >&2
    echo "  rsync ext/exo/dashboard/build/ to $EXO_REPO/dashboard/build/" >&2
    exit 1
fi

cd "$EXO_REPO"

export EXO_MODELS_DIRS EXO_LIBP2P_NAMESPACE EXO_OFFLINE

echo "Launching exo from $EXO_REPO"
echo "  models:    $EXO_MODELS_DIRS"
echo "  namespace: $EXO_LIBP2P_NAMESPACE"
echo "  offline:   $EXO_OFFLINE"
echo "  API will listen on http://0.0.0.0:52415"
echo

exec uv run exo "$@"
