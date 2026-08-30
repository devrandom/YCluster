# `vm launch`: preflight memory check before starting a GPU VM

GPU-passthrough VMs pin their entire guest RAM upfront (VFIO cannot
overcommit), so a `vm launch` on a host without enough un-pinned memory
can wedge the whole machine.

`ycluster vm launch` currently checks GPU availability but not host
memory. The pilot check should be a hard preflight, not a warning:

## Proposed behavior

In `vm_launch` (and `vm_start`, since the reconciler can start a VM
scheduled on):

1. Read `MemAvailable` from `/proc/meminfo` on the host.
2. Parse the requested `--mem` (e.g. `32GiB`) into bytes.
3. If `MemAvailable < requested + margin` (margin ≈ 4 GiB for host
   services, page cache churn during image unpack, and Incus overhead),
   **fail the launch** with a message that names the constraint:
   ```
   Refusing to launch 'example-vm' (32GiB): host MemAvailable is 21GiB,
   need at least 36GiB. Running VMs pin their RAM (VFIO); free one or
   lower --mem.
   ```
4. The check must count *currently running* instances' pinned RAM, not
   just the requested amount: iterate `incus list` for VMs in the
   `Running` state on this host, sum their `limits.memory`, and
   require `MemAvailable ≥ requested + Σ(pinned by others) + margin`.
   (A host may look healthy before the guest has finished faulting in
   its pinned pages — the failure mode is gradual as QEMU faults in
   guest memory.)

## Design considerations

- **Sum-of-running vs MemAvailable:** checking only `MemAvailable` at
  launch time is racy — another VM's QEMU may not have finished
  faulting in its guest RAM yet. The robust invariant is
  `Σ(running VMs' limits.memory) + requested + host_overhead ≤ total
  RAM`; enforce it at launch/start time.
- **Stopped VMs don't count** — but only if they are truly stopped.
  The reconciler's "desired state" and Incus's actual state can
  diverge; always read `incus list`, not etcd records, for the
  running set.
- **Containers are cheap** — exclude them from the pinned-memory sum
  (only `type=virtual-machine` pins RAM).
- **Escape hatch:** a `--force` flag (or an env var) to bypass the
  check for exceptional cases, logged to the VM's etcd record so
  bypasses are auditable.
- **Reconciler needs the same guard:** `vm-reconciler.service` calls
  `vm_start` on schedule; a scheduled start can hit the same
  overcommit. Same preflight, same failure message, recorded as a
  reconciler issue rather than an exception.

## Done when

- `vm launch` refuses to start a VM whose memory would overcommit the
  host, with a clear error naming the deficit.
- `vm start` (reconciler path) does the same check and surfaces the
  failure as a per-VM issue rather than wedging the host.
- A test on the dev cluster covers the "second VM doesn't fit" case.
