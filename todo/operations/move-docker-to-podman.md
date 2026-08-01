# Migrate storage-node services from docker to podman

Docker is installed on `s1`, `s2`, `s3` (Ubuntu 24.04 `docker.io`
package, plus `moby/buildkit` for the in-cluster registry builder) and
runs services that should be on podman for consistency with the rest
of the cluster. Podman is already standard on `c1-c3` and `nv1-nv6`
(see `admin/install-podman.yml`).

## Current docker footprint

| Host  | Service                          | Image                                       |
|-------|----------------------------------|---------------------------------------------|
| s1,s2,s3 | grafana                       | grafana/grafana:latest                      |
| s1,s2,s3 | buildkitd                     | moby/buildkit:buildx-stable-1               |
| s3    | docker-registry                  | registry:2                                  |
| s3    | open-webui (leader-elected)      | registry.xc:5000/open-webui-with-plugins    |
| s3    | open-webui-stage (leader-elected)| registry.xc:5000/open-webui-with-plugins    |
| s3    | authentik-server, authentik-worker | ghcr.io/goauthentik/server:2026.2.4      |
| s1    | valkey/redis                     | valkey/valkey:8-alpine                      |

`meshcommander` and `searxng-docker` (also `caddy`) were removed
2026-08-01 via `admin/remove-meshcommander.yml` + manual cleanup
(see `searxng-cleanup.md` archived).

## Why

- Docker is the only engine on storage; every other host type uses
  podman. Two engines to maintain, two sets of storage paths to prune,
  two upgrade stories.
- Storage's open-webui/authentik/registry already publish to
  `registry.xc:5000` and are pulled via `podman pull` on the consumer
  side; using podman for the server side makes the registry
  homogeneous.
- Buildkitd-on-every-node duplicates the build cache (RBD-backed
  registry already persists built images).

## Plan

1. **Leader-elected services first** (already single-instance, so
   storage topology change is minimal):
   - `open-webui`, `open-webui-stage`, `authentik-server`,
     `authentik-worker` → switch the systemd unit's `ExecStart` from
     `docker run` to `podman run --replace` (with the
     `--network=host`-equivalent `--net slirp4netns` or host net
     depending on current wiring).
2. **Registry + buildkitd**:
   - Replace `docker-registry` systemd unit with a podman quadlet /
     systemd unit pointing at the same RBD volume
     (`/rbd/misc/docker-registry/data`).
   - Move buildkitd behind the registry: one shared buildkit
     reachable by TCP, or drop the local buildkit in favour of
     `buildah bud` (already on every host) pushing directly to the
     registry. Storage's per-node buildkit is the easiest target.
3. **grafana**: trivially one image, three replicas. Per-host
   state at `/var/lib/grafana-docker` already diverges (sqlite on
   each node); re-evaluate when this play lands if grafana
   consistency becomes a real problem.
4. **valkey**: trivial — single container, port 6379.
5. **Uninstall docker** once 1-4 are stable. Drop
   `install-docker-official.yml` from `admin.yml`, keep
   `docker-image-pruning.md` retired (no more docker layers).

## Risks

- `docker-registry` keeps its data dir on RBD; switching engine must
  preserve `/rbd/misc/docker-registry/data` byte-for-byte (registry:2
  data layout is engine-agnostic).
- The open-webui `bash start.sh` entrypoint expects `/dev/dri` or
  similar host paths; verify podman preserves them under the new
  container spec (podman 5.8.0, see `admin/install-podman.yml`).
