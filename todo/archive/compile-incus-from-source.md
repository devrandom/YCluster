# Compile Incus from source (drop noble's 6.0.0 zlib publish bottleneck) — DONE

## Status — DONE

Built Incus 6.0.5 from source and swapped it into place on the first
production VM host (`config/ansible/admin/build-incus.yml` +
`install-incus.yml`: build vendored under `/opt/src/incus/bin`, distro
binaries `dpkg-divert`ed aside, apt `incus`/`incus-client` packages kept
for the units/groups/man-pages). Verification on that host:

- `incusd --version` reports 6.0.5.
- The unified `incus publish --compression zstd` no longer passes `-c`:
  the inner convert is `qemu-img convert ... -O qcow2 -T none -t none` and
  the outer zstd archive does all the compression.
- The CUDA/vLLM image republish dropped from ~20+ min (6.0.0 zlib path) to
  ~2 min.
- Both GPU guest images were rebuilt and verified against the 6.0.5 build
  (`ubuntu-cuda`, `ubuntu-cuda-vllm`); the old orphan vLLM image was
  removed. Existing VMs are unaffected (independent root volumes, not
  overlays).

Rolling 6.0.5 out to the remaining Incus hosts (after the nv1 soak) is a
HIGH deployment-specific item in `ycluster-private/todos.md`.

Why: noble's apt `incus` is 6.0.0, which hardcodes the VM publish
convert as `qemu-img convert -f raw -O qcow2 -c` (single-threaded zlib)
and offers no `-o compression_type` knob. That step is the long pole in
`incus publish` for the CUDA/vLLM VM images (~20+ min each). The fix
landed in Incus >= 6.0.5: for **unified** publishes the inner convert no
longer passes `-c`, so the (multithreaded) outer `--compression zstd`
archive does all the compression. We already publish unified with
`--compression zstd`, so upgrading past 6.0.0 removes the bottleneck with
no workflow change.

There's no apt path to it: noble holds the 6.0 line at 6.0.0
(`6.0.0-1ubuntu0.3`), and resolute only carries 6.0.5-8 on the same 6.0
LTS line. Consistent with the "no prebuilt third-party binaries" rule
and the existing `build-podman.yml` precedent, build Incus from source.

Verified against `/ext/incus` (gitignored scratch clone; `v6.0.5` and
`v7.0` tags checked): 6.0.5 has the fix (`rootfsWriter == nil` for
unified VM export → no `-c`); noble ships the `libcowsql-dev` cgo dep
(1.15.4). Still hardcodes zlib for *split* publishes (we don't use split).

## Plan
1. `config/ansible/admin/build-incus.yml` mirroring `build-podman.yml`:
   - Pin either `v6.0.5` (6.0 LTS, small delta from current) or `v7.0`
     (current LTS, forward-looking) — decide when implementing.
   - Buildhost (same as podman), source vendored under `/opt/src/incus`.
   - Apt build deps (from Incus CI): `make build-essential git
     autoconf automake libtool pkg-config libacl1-dev libcap-dev
     libdbus-1-dev liblz4-dev libseccomp-dev libselinux-dev
     libsqlite3-dev libudev-dev libuv1-dev lxc-dev libcowsql-dev zip`,
     plus a Go toolchain (apt `golang-*`, same as podman/`local_ai_proxy_go_bin`).
   - `make deps` (builds cowsql raft C lib), set
     `CGO_CFLAGS/-LDFLAGS`/`LD_LIBRARY_PATH` per upstream `build.yml`,
     then `make`. Verify `incusd --version` and collect
     `incusd`/`incus` (+ `incus-agent`, `incus-migrate`).
2. `config/ansible/admin/install-incus.yml`: swap the `apt install incus`
   for deploying the built `incusd`/`incus` (keep apt for `incus-tools`,
   `qemu-system-x86`, `ovmf`, `virtiofsd`). Binary-only swap into the
   existing unit/data/storage.
3. Roll out on a production VM host with an Incus daemon restart (prod-impactful — needs
   explicit go-ahead). Sanity: re-publish a small VM image and confirm
   the `qemu-img convert` step no longer carries `-c`, then time the
   CUDA/vLLM rebuild.

## Acceptance
- [x] `incus publish <vm> --compression zstd` (unified) shows
  `qemu-img convert ... -O qcow2 -T none -t none` with no `-c`.
- [x] Image publish wall-time drops (the zlib step disappears; outer zstd
  unchanged). Rebuild/republish `ubuntu-cuda-vllm` and compare.

## Refs
- `/ext/incus` — `internal/server/instance/drivers/driver_qemu.go`
  (Export/convert), `cmd/incusd/images.go` (unified vs split),
  `.github/workflows/build.yml` (CI build steps).
- Docs: `docs/operations/packages.md` (source-build/`/opt/src` policy),
  `docs/operations/cluster-exec.md`.
