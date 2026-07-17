# GPU-VM DKMS → Canonical-signed precompiled modules (optional)

Replace the GPU-VM guests' DKMS driver build with Ubuntu's
Canonical-signed precompiled modules to drop DKMS entirely. Eval'd
2026-06-13, viable — the `linux-headers-virtual` fix that made DKMS
reliable is already shipped, see `CHANGELOG.md`.

## Current state

`incus-build-gpu-image.sh.j2` currently installs `nvidia-open` from
NVIDIA's CUDA repo + DKMS (`nvidia-dkms-open`, `dkms`,
`linux-headers-virtual`).

## Proposed

Ubuntu's `noble-updates/restricted` ships
`linux-modules-nvidia-595-open-generic` — a tracking meta that follows
the generic kernel ABI in lockstep (no DKMS, no reboot-driverless risk,
secure-boot-signed; the guests' `linux-image-virtual` resolves to the
generic ABI, so `-generic` matches) — and the matching userspace
`nvidia-driver-595-open` is the *identical* point release (595.71.05) we
run today, so no version regression on branch 595.

Switch shape: drop `dkms`/`nvidia-dkms-open`/`linux-headers-*`, install
`linux-modules-nvidia-<branch>-open-generic` + `nvidia-driver-<branch>-open`
(templated branch var) from the archive, keep `cuda-toolkit-X` +
`nvidia-container-toolkit` from the CUDA repo.

## Trade-off

Pins the driver to Ubuntu's restricted cadence — branch 610 (already in
the CUDA repo) isn't precompiled yet, so chasing the newest driver for
new-GPU support would need to fall back to the DKMS path.

## Before rollout, verify

- CUDA-toolkit/userspace compat
- `linux-image-virtual`→generic-ABI meta tracking actually pulls the
  new module across a real kernel bump
