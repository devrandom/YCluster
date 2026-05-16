#!/usr/bin/env bash
# Build the exo dashboard (svelte static site) in a throwaway podman
# container so we never run npm on the macOS inference nodes. Output
# lands in <exo-checkout>/dashboard/build/.
#
# Usage:
#   build-exo-dashboard.sh <exo-checkout-dir>
#   EXO_SRC=/path/to/exo build-exo-dashboard.sh
#
# The exo checkout must be a clean tree of exo-explore/exo at the commit
# being deployed. setup-exo.yml calls this on the control host; it can
# also be run by hand.
#
# Podman rootless maps container-root to the invoking host user, so the
# build output comes back owned by you — no daemon or root needed.

set -euo pipefail

exo_src="${1:-${EXO_SRC:-}}"
if [[ -z "$exo_src" ]]; then
    echo "Usage: $0 <exo-checkout-dir>   (or set EXO_SRC)" >&2
    exit 1
fi

dashboard="$exo_src/dashboard"
if [[ ! -f "$dashboard/package.json" ]]; then
    echo "Error: $dashboard/package.json missing — $exo_src is not an exo checkout" >&2
    exit 1
fi

# -t for streaming output. :Z is a no-op on non-SELinux hosts but keeps
# the script portable. npm ci insists on an empty node_modules, so we
# let it manage that inside the container.
podman run --rm -t \
    -v "$dashboard:/work:Z" \
    -w /work \
    docker.io/library/node:20 \
    bash -c 'npm ci && npm run build'

echo
echo "Built: $dashboard/build"
du -sh "$dashboard/build"
