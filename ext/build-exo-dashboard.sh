#!/usr/bin/env bash
# Build the exo dashboard (svelte static site) in a throwaway rootless
# podman container so we never run npm on the macOS inference nodes.
# Output lands in ext/exo/dashboard/build/.
#
# Podman rootless maps container-root to the invoking host user, so the
# build output comes back owned by you. No daemon, no root privileges
# needed on the build host.
#
# Assumes ./ext/exo is a clean git checkout of exo-explore/exo at the
# commit you want to deploy.
#
# After a successful build, distribute the artifact:
#
#   rsync -av --delete ext/exo/dashboard/build/ dev@m1.yc:exo/dashboard/build/
#   rsync -av --delete ext/exo/dashboard/build/ dev@m2.yc:exo/dashboard/build/
#
# (Keep m1 and m2 on the same exo git commit or the dashboard may drift
# from the backend.)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dashboard="$here/exo/dashboard"

if [[ ! -f "$dashboard/package.json" ]]; then
    echo "Error: $dashboard/package.json missing — clone exo into $here/exo first:" >&2
    echo "  git clone https://github.com/exo-explore/exo.git $here/exo" >&2
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
