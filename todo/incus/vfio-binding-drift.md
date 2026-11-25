# A passthrough GPU can lose its vfio binding and vanish from the pool

`gpu-passthrough.service` applies the passthrough split once at boot,
per declared address. Nothing re-applies it at runtime. When a PCIe
hotplug event on a card's root port removes and re-enumerates the
device (link drop; the slot reports the card absent and then present),
the new `pci_dev` starts with an empty `driver_override`. On a host
where `nvidia` is blocked by `nvidia-deferred.conf`, the GPU function
then re-probes to no driver at all, while its HD-Audio sibling (`.1`)
falls back to `snd_hda_intel`. The card is healthy; only its binding
is gone.

The CLI cannot see that state. `gpu_pool()` enumerates
`/sys/bus/pci/drivers/vfio-pci`, so an unbound card is not reported as
`WEDGED` (bound but unresponsive) — it is absent: `vm_gpus()` prints
one fewer line, `free_gpus()` never offers it, `_gpu_healthy()` never
probes it, and `host_snapshot()` reports the smaller bound count as
`gpu_pool`. Admission control and the schedule page then treat that as
the host's real capacity, and a start that needs the missing card
fails with `Only N healthy GPU(s) free` with nothing pointing at the
cause. The declared truth (`incus_passthrough_gpu_pci` in the private
host vars) is never compared against what is actually bound.

Recovery today is manual and documented in
[`docs/operations/vm-hosting.md`](../../docs/operations/vm-hosting.md#recovering-a-passthrough-gpu-that-lost-its-vfio-binding):
`sudo systemctl restart gpu-passthrough.service` rebinds driverless
cards (the script is idempotent per device and refuses if it finds a
card on `nvidia`). That fixes the symptom but leaves the next flap
just as invisible.

## Proposed

1. Render the declared function list to a host file (e.g.
   `/etc/ycluster/gpu-passthrough.list`) in `install-incus.yml`, from
   `incus_passthrough_gpu_pci` plus the derived `.1` siblings — in the
   same task that renders the bind script, so the script and the CLI
   read one source.
2. Read it in `vm_manager.py` and report drift in `ycluster vm gpus`:
   a declared address whose GPU function is not bound to `vfio-pci`
   prints as `MISSING (declared, not on vfio-pci)`, distinct from
   `WEDGED`. Fail soft when the file is absent (older hosts, dev
   cluster).
3. Expose the mismatch in the sampler's host snapshot (e.g.
   `gpu_pool_declared` alongside `gpu_pool`) so the schedule page and
   admission control can flag a host whose live pool is short, while
   continuing to schedule against the live pool.
4. Once the visibility exists, a udev rule (or systemd path unit)
   that re-binds a declared card on PCI `add`, but **only** when the
   device is driverless or on a non-`nvidia` driver, reusing the bind
   script's BAR1 shrink. A re-enumerated card comes back at the 256
   MiB default, so a VM started on an unshrunk card maps a small BAR.

## Constraints

- Never rebind a card from `nvidia` to `vfio-pci` at runtime: it hangs
  in-kernel. The bind script already refuses; any new path must too.
- Never force-stop a running GPU VM to "free" a card.
- The declared list is host-local state derived from the private host
  vars; it must not become repo content.

## Acceptance

On a host with a declared but unbound card: `ycluster vm gpus` names
it as missing, the snapshot shows live < declared, and no VM start
silently assumes the larger pool. Unit tests follow the mocked-sysfs
pattern in `tests/test_vm_scheduling.py`; a dev-cluster test unbinds a
spare card and asserts both outputs.
