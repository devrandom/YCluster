#!/usr/bin/env bash
# Deploy exo to the macOS inference nodes.
#
# For each host:
#   1. Ensure ~/exo exists, fetching from origin if needed.
#   2. Check out the same commit that ext/exo is on (so the dashboard
#      we ship matches the backend running on the mac).
#   3. rsync ext/exo/dashboard/build/ into ~/exo/dashboard/build/.
#   4. Copy run-exo.sh into the home directory.
#
# Run from the admin host after ./build-exo-dashboard.sh has produced
# a fresh dashboard/build/ artifact. npm / node / node_modules never
# touch the macs.
#
# Override the target host list with EXO_HOSTS, e.g.
#   EXO_HOSTS="m1.yc m3.yc" ./deploy-exo.sh

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src_repo="$here/exo"
build="$src_repo/dashboard/build"
launcher="$here/run-exo.sh"
exo_remote_url="${EXO_REMOTE_URL:-https://github.com/exo-explore/exo.git}"
hosts="${EXO_HOSTS:-m1.yc m2.yc}"

if [[ ! -d "$src_repo/.git" ]]; then
    echo "Error: $src_repo is not a git checkout" >&2
    exit 1
fi
if [[ ! -f "$build/index.html" ]]; then
    echo "Error: $build/index.html missing — run ./build-exo-dashboard.sh first" >&2
    exit 1
fi
if [[ ! -x "$launcher" ]]; then
    echo "Error: $launcher missing or not executable" >&2
    exit 1
fi

pinned_commit="$(git -C "$src_repo" rev-parse HEAD)"
echo "Pinning macs to exo commit $pinned_commit"
echo "Target hosts: $hosts"
echo

for host in $hosts; do
    echo "==> $host"

    # Ensure the checkout exists and is at the pinned commit. We fetch
    # by sha directly so branches don't have to match.
    ssh "dev@$host" bash -s <<EOF
set -euo pipefail
if [[ ! -d ~/exo/.git ]]; then
    git clone "$exo_remote_url" ~/exo
fi
cd ~/exo
git fetch origin "$pinned_commit" || git fetch origin
git checkout --detach "$pinned_commit"
EOF

    # Ship the prebuilt dashboard. --delete keeps the dir tight; no
    # other writer touches it on the mac.
    rsync -av --delete "$build/" "dev@$host:exo/dashboard/build/"

    # Drop the launcher in ~/. It's re-synced every deploy so local
    # edits on the mac will be overwritten — edit in the repo instead.
    rsync -av "$launcher" "dev@$host:~/run-exo.sh"

    echo
done

echo "Done. Start exo manually on each mac with: ./run-exo.sh"
