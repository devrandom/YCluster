# ycluster vm set-gpus

Add a `ycluster vm set-gpus <name> <count>` subcommand. Changing a VM's
GPU count is currently a manual procedure: graceful `ycluster vm stop`,
`incus config device remove <name> gpuN ...` (or `device add` from
`free_gpus()` when growing), `ycluster vm start`, then a hand-edited
etcd write to update `gpus` in `/cluster/vms/<name>` — easy to get wrong
or forget the registry half.

The subcommand belongs in `vm_manager.py` (reuse the launch path's pool
selection and device-naming convention, keep `gpu0..gpuN-1` contiguous)
+ `cli/vm.py`.

## Pitfalls to handle

1. **Physical passthrough can't hot-(un)plug** — require/perform a
   stop/start.
2. **Restart can boot into a stale kernel** staged by unattended-upgrades
   since its last boot, with no NVIDIA module built for it — verify the
   guest sees the expected GPU count (`nvidia-smi -L` via incus exec)
   after start and warn loudly.
