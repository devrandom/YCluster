# VM scheduling iteration 3: web-driven VM creation

Design in `docs/design/vm-usage-scheduling-accounts.md` Phase 5; depends
on iteration 2, which is done. Record the new VM in the existing
`/cluster/vms/<name>` registry (no separate intent prefix) with a
`state: requested` gate — the per-host vm-reconciler already iterates
`vms_all()` filtered to its host, so it gains a branch: record present +
no local incus instance + `state == requested` → create.

Gate on `state` (requested → provisioning → ready), never on mere
absence, or a hand-`incus delete`d VM with a stale record gets silently
resurrected; a `ready`/running record whose instance vanished stays an
anomaly (vm_start → "instance not found" → `/cluster/vm-issue/`), not a
recreate.

## Key design decisions

1. **Provision GPU-less** — `vm_launch`'s slow tail (wait-agent,
   cloud-init, sshd, key inject, bastion sync) touches no GPU; the
   ~900 s wait is purely OVMF enumerating passed-through GPU BARs, so
   init without GPU devices boots in ~180 s, leaves the pool free during
   provisioning, and lands the VM **stopped with GPUs released** = the
   scheduler's steady state. GPUs attach only on the first real start
   via the existing `_ensure_gpus_attached`.
2. **Detached provision task** — ~180 s still exceeds the 120 s tick /
   300 s `TimeoutStartSec`, so the reconciler spins off a
   `vm-provision@<name>.service` (longer timeout) and returns; state
   field blocks re-entry; a crash leaves `provisioning` for an
   interrupted-recovery branch to clean up + relaunch.
3. **Registry record gains** `cpu`/`mem`/`image` (launch-args-only today).
4. **Web picks `host` at create time** using the iteration-2 pool sizes
   (`gpu_pool` in sampler snapshots) + `gpu_commitments`; same
   `gpu_conflict` admission check runs at create so over-capacity is
   rejected on the page.
5. **Admin-gated until quotas** (Phase 4) — no per-owner budget to admit
   self-serve creation against yet. Pre-flight: owner has SSH keys
   (`vm_launch` requires them), name free, capacity fits.

## Empirical check before relying on it

A guest provisioned with zero GPUs then booted with GPUs attached must
bind its baked-in nvidia driver cleanly (should — same attach-then-boot
ordering `_ensure_gpus_attached` + start already uses — but verify on a
real passthrough VM).
