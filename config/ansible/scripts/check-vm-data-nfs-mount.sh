#!/bin/bash
# check-vm-data-nfs-mount.sh — assert the Incus VM-data NFS mount is present.
#
# Scripts that touch VM `data` passthrough paths call this before writing so a
# missing mount fails fast instead of writing into an empty/missing directory
# (which would leave the guest disk silently disconnected). Installed by
# mount-nas-share.yml at /usr/local/bin/check-vm-data-nfs-mount.
#
# Usage: check-vm-data-nfs-mount.sh [MOUNT]
#   MOUNT   mount point to verify (default: /mnt/near1-vm, override via
#           VM_DATA_NFS_MOUNT env)

set -euo pipefail

mount="${1:-${VM_DATA_NFS_MOUNT:-/mnt/near1-vm}}"

if ! mountpoint -q "$mount"; then
    echo "check-vm-data-nfs-mount: $mount is not an active mount" >&2
    exit 1
fi
