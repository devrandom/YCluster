# Build incus VM images once, copy between hosts

Today each VM host runs `incus-build-gpu-image` + `incus-build-vllm-image`
independently (~1h each, duplicated 8-CPU/16GiB builder load, and the
"identical" images actually differ per host — different build dates pick
up different package versions).

Incus supports server-to-server image transfer (`incus image copy` or
export/import), so the playbook could build on one host and distribute:
pick a build host, run the helpers there, copy to the rest, keeping
aliases in sync.

## Prerequisite to check

Image transfer needs the incus API listening between hosts (or fall back
to `incus image export` + scp + import, which works with no API
exposure).

## Trade-off

Per-host builds do have one virtue — no cross-host dependency — so keep
the standalone helpers as the fallback path. Worth doing before the image
set grows (more derived layers / bigger AOT caches make duplicate builds
increasingly wasteful).
