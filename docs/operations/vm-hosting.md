# GPU VM Hosting

YCluster runs GPU-passthrough virtual machines on designated Nvidia
nodes via [Incus](https://linuxcontainers.org/incus/). VMs get one or
more whole GPUs by PCI passthrough, an isolated NAT network, and
optional external SSH access. The `ycluster vm` CLI manages them.

- **Hosts**: any node with `incus_vm_host: true` in its host_vars
  (currently `nv2`, `nv3`).
- **Playbook**: `admin/install-incus.yml` — ends early on non-VM-hosts.
- **Images**: `ubuntu-cuda` and the derived `ubuntu-cuda-vllm` — see
  [Images](#images).

## Adding a new VM host

Onboarding a new GPU VM host touches several independent pieces, and it is
easy to miss one (a missing frontend firewall rule silently drops all
external SSH to VMs on the new host). Work through them in order:

1. **Register the node in etcd so the inventory sees it.** The etcd
   inventory plugin (`config/ansible/inventory_plugins/etcd_nodes.py`)
   reads node allocations under `/cluster/nodes/by-hostname/…`; a GPU VM
   host must be allocated with `type: nvidia`. `nvidia` is a child of
   `compute`, so this is what puts the host in the `compute` group that the
   VM plays target — and in `groups['compute']`, which
   `app/install-frp-server.yml` iterates to compute frontend firewall
   ports.

2. **Add `host_vars/<host>.yml`** (in `ycluster-private`, symlinked in):
   ```yaml
   incus_vm_host: true
   incus_passthrough_gpu_pci:
     - "0000:<bus>:<slot>.0"        # primary PCI function of each passthrough GPU
   vm_bastion_service: vm-bastion-<host>
   ```
   `vm_bastion_service` is what enables external SSH and what the frontend
   firewall logic keys on.

3. **Physical prerequisites.** Enable AMD-Vi / IOMMU in the BIOS (see
   [BIOS: enable AMD-Vi (IOMMU)](#bios-enable-amd-vi-iommu)) so `vfio-pci`
   can bind the passthrough GPUs.

4. **Run the provisioning playbooks** (as `root` via Ansible, on the
   `managed`/compute host):
   - `admin/install-incus.yml` — installs Incus, builds/publishes the VM
     images, creates the `incusbr0` bridge + isolation ACL, and installs
     `gpu-passthrough.service`. **Reboot the host after the first run** so
     the GPU split (nvidia vs `vfio-pci`) takes effect at boot — see
     [Critical rule: never live-rebind a GPU](#critical-rule-never-live-rebind-a-gpu).
   - `admin/install-vm-bastion.yml` — deploys the `bastion` container
     (jump-only sshd + frpc) and derives the host's
     [`bastion_public_port`](#port-derivation).

5. **Update the frontend host firewall — easy to forget.** Re-run
   `app/install-frp-server.yml` against the `frontend` group
   (currently `dev1a`/`dev1b`) so `ufw` opens the new host's derived
   bastion public port (frontends default-deny incoming). Without this
   step, external SSH through the new host's bastion is silently dropped.
   `frps` runs on the frontends, so it forwards the public bastion port to
   the host's bastion tunnel.

6. **Verify.**
   - The host appears in the inventory under `compute`/`nvidia`
     (`ansible-inventory --host <host>`).
   - A frontend `ufw status` shows the derived public port allowed, and
     re-running `install-frp-server.yml` shows a `changed` for that port
     on first fix-up.
   - `frpc-bastion.service` is active in the bastion, and
     `ycluster vm bastion-sync` has filled its `authorized_keys`.
   - Boot a test VM and `ssh -J jump@<frontend>:<port> ubuntu@<vm>`.

### Port derivation

The public bastion port is derived from the node name and index, and the
derivation is duplicated in both `admin/install-vm-bastion.yml`
(`bastion_public_port`) and `app/install-frp-server.yml`
(`vm_bastion_public_ports`) — keep them in sync:

| node | idx ≤ 3 | idx > 3 |
|------|---------|---------|
| nvidia (`nv<idx>`) | `2210+idx` (nv1→2211) | `2220+idx` |
| compute (`c<idx>`)  | `2213+idx` (c1→2214) | `2240+idx` |

Low-index ports are historical and stable (existing `ssh -J` invocations
keep working); the disjoint `2220+idx` / `2240+idx` blocks for idx > 3
mean adding one node type never shifts another type's ports.

## GPU Split (host vs. passthrough)

Each VM host divides its GPUs: some stay on the `nvidia` driver for the
host (e.g. a local vLLM), the rest are bound to `vfio-pci` and handed to
VMs. The passthrough set is declared per host:

```yaml
# host_vars/nv3.yml
incus_vm_host: true
incus_passthrough_gpu_pci:
  - "0000:c1:00.0"
  - "0000:e1:00.0"
```

A GPU passes through together with its HD-Audio function (`.1`): they
share an IOMMU group, so vfio requires the whole group.

### BIOS: enable AMD-Vi (IOMMU)

Whole-GPU passthrough needs AMD-Vi / IOMMU groups; without them
`vfio-pci` probe fails (`error -22`). On AMD EPYC hosts with AMI
Aptio (ASRock Rack boards, e.g. Comino ONYX) the toggle is **not** in
plain CPU Configuration — it's under AMD CBS:

- **Advanced → AMD CBS → NBIO Common Options → IOMMU** → Enabled
- SVM Mode (a separate CPU-virt toggle) is in the same CBS tree.

After enabling, reboot and verify:

- `cat /sys/firmware/acpi/tables/IVRS | wc -c` — non-zero
- `dmesg | grep -i amd-vi` — `IOMMU performance counters supported` per I/O die
- `ls /sys/kernel/iommu_groups/` — populated

Without an exposed IVRS table, no kernel cmdline (`amd_iommu=on`,
`iommu=force`, etc.) will enable it — the BIOS has to publish the
hardware description.

### How the split is applied

`gpu-passthrough.service` runs `/usr/local/sbin/gpu-passthrough-bind`
once at boot:

1. For each passthrough PCI function: write `vfio-pci` to the device's
   `driver_override` and `drivers_probe` it (**address-specific**). Each
   GPU's Resizable BAR is also shrunk here — see
   [Large-BAR GPUs](#large-bar-gpus-and-vm-boot-time).
2. `modprobe --ignore-install nvidia` (+ `nvidia_modeset/uvm/drm`) — the
   host GPUs bind nvidia; the passthrough GPUs are skipped because their
   `driver_override` forces `vfio-pci`.

`/etc/modprobe.d/nvidia-deferred.conf` contains `install nvidia /bin/false`
(and submodules). This blocks **every** nvidia load path — including
explicit `modprobe` by services like `nvidia-cdi-refresh` — so nvidia
cannot claim a passthrough GPU before the bind script runs. The script
uses `modprobe --ignore-install` to bypass that guard deliberately.

### Critical rule: never live-rebind a GPU

Moving a GPU function between `nvidia` and `vfio-pci` **while the host
is running** hangs in-kernel: the process is stuck holding the device
`device_lock`, unkillable even by `SIGKILL`. Only a reboot clears it.

- Passthrough binding takes effect **only at boot**, via the service.
- After `install-incus.yml` first runs, the host **must be rebooted**
  for the GPU split to take effect.
- The bind script refuses to run if it finds a passthrough GPU already
  on `nvidia` — it errors out rather than attempt the rebind.

The same wedge is also triggered by **force-stopping a running GPU VM**
— `incus stop --force` / `incus delete --force` SIGKILL qemu mid GPU
reset and leave the device in `vfio_pci_core_disable`. Only the *clean*
path is safe: `incus stop` (no `--force`) and then `incus delete`, or
just use `ycluster vm destroy` which does this for you. Never `--force`
a running GPU VM.

If a shutdown hangs on a wedged process (and the BMC NIC is unwired, so
there is no out-of-band console), reboot via sysrq from a live shell:

```bash
echo s | sudo tee /proc/sysrq-trigger   # sync
echo u | sudo tee /proc/sysrq-trigger   # remount read-only
echo b | sudo tee /proc/sysrq-trigger   # reboot now
```

### Why not driverctl

driverctl binds `vfio-pci` by **device-id** (`new_id`). Every GPU in
these hosts is the identical model, so a device-id bind sweeps up the
host GPUs too. Address-specific `driver_override` is the only way to
split identical cards. `driverctl` is removed by the playbook.

### Large-BAR GPUs and VM boot time

These GPUs expose a **128 GiB Resizable BAR** (the full VRAM aperture).
A VM's OVMF firmware must lay out and map every passed-through BAR
before the guest kernel starts — with two 128 GiB BARs that took
**~10 minutes**. The bind script therefore shrinks each passthrough
GPU's BAR to **1 GiB** (writing the size index to `resourceN_resize`
while the device is unbound), which cuts VM firmware boot to ~90 s.
Inference is unaffected — benchmarked identical at 1 GiB vs 128 GiB:
once weights are resident, generation is GPU-internal, not BAR-bound.

### GPU power limiting

The host's `nvidia-power-limit.service` (default **300 W**, set by
`admin/install-nvidia.yml`) only sees host-visible GPUs. On a passthrough
host those GPUs are bound to `vfio-pci` and handed to VMs, so the host
service correctly no-ops there — **the 300 W cap therefore must be applied
inside each guest**, or the passthrough GPUs run at the card's default
(600 W) limit.

The guest image enforces this: `install-incus.yml` bakes
`nvidia-power-limit` plus a `nvidia-power-limit.service` (same default
300 W) into `ubuntu-cuda`, so every GPU VM caps its GPUs on every boot.
The `nvidia_power_limit_watts` var in `install-incus.yml` controls both
the value and whether guests get `--max` (matches
`install-nvidia.yml`'s host-side default).

To change an **already-running** VM before the next image rebuild, set it
by hand (lost on guest reboot until the VM is re-provisioned from a
rebuilt image):

```bash
incus exec <vm> -- nvidia-smi -pl 300
```

## Managing VMs

The `ycluster vm` CLI runs on the VM host (it shells out to `incus`
and reads/writes etcd).

```bash
# SSH key registry — a user must have a key before owning a VM
ycluster vm ssh add alice@example.com 'ssh-ed25519 AAAA...'
ycluster vm ssh list [alice@example.com]
ycluster vm ssh remove alice@example.com '<key or substring>'

# VM lifecycle
ycluster vm launch dev1 --owner alice@example.com --gpus 1 --cpu 8 --mem 32GiB
ycluster vm launch cpu1 --owner alice@example.com --gpus 0     # CPU-only VM
ycluster vm list
ycluster vm stop|start|destroy dev1
ycluster vm resize dev1 160GiB        # grow the root disk
ycluster vm gpus                      # passthrough GPU allocation

# Bastion access list (regenerated automatically on key/VM changes)
ycluster vm bastion-sync
```

VMs are **persistent and owned** — not ephemeral. One user may own many
VMs; a VM has exactly one owner. Owner SSH keys are injected into the
guest automatically and refreshed on `ssh add/remove`. Extra registered
users can be granted SSH access to the same VM without becoming its owner:

```bash
ycluster vm alter dev1 --add-user bob@example.com     # grant access
ycluster vm alter dev1 --remove-user bob@example.com  # revoke access
```

Additional users get the same SSH access as the owner (their keys are
injected into the guest and they can tunnel through the bastion to that
VM), but the owner remains the billing/accounting identity.

### etcd layout

- `/cluster/users/<user>` — `{"ssh_keys": [...]}`
- `/cluster/vms/<name>` — `{"owner", "additional_users", "gpus", "host",
  "created", "state"}`.
  `additional_users` is an optional list of registered users granted SSH
  access to this VM alongside the owner. `state` is `provisioning` while a
  launch is in progress and `ready` once it completes — so an interrupted
  launch is recognisable. `host` records which VM host the VM runs on
  (`vm list` shows runtime state only for the local host's VMs; others
  show `(remote)`).

### Disk resize

`ycluster vm resize` overrides the profile root-disk size, then restarts
the VM (Incus only applies a new size on start). cloud-init grows the
partition and filesystem on boot. Grow-only — Incus cannot shrink.

## Images

Two layered VM images, built on the host and published to the local
Incus image store:

- **`ubuntu-cuda`** — Ubuntu 24.04 + NVIDIA open driver + CUDA toolkit,
  plus the build tools vLLM needs to JIT-compile kernels at runtime
  (`python3-dev`, `ninja-build`, `cmake`). Built by `incus-build-gpu-image`.
- **`ubuntu-cuda-vllm`** — `ubuntu-cuda` + vLLM preinstalled as a `uv`
  tool, plus the FlashInfer SM120 kernel cache AOT-baked in (see
  [Pre-warm the FlashInfer kernel cache](#pre-warm-the-flashinfer-kernel-cache)).
  Built by `incus-build-vllm-image`, a layered build from `ubuntu-cuda`.
  Kept as a separate image so a vLLM bump does not force a full CUDA
  rebuild. `uv` is installed pinned and SHA256-verified from PyPI
  (`uv_version` / `uv_sha256` playbook vars) — not the third-party
  `astral-uv` snap, which auto-refreshes.

Versions are playbook vars in `install-incus.yml`: `cuda_version`,
`vllm_version`, `uv_version` / `uv_sha256`, `flashinfer_version`.

### Building / rebuilding

`install-incus.yml` builds whichever image is missing (idempotent —
existing-alias check skips the work on later runs). To target just the
image-build step:

```bash
# build missing image(s) only
ansible-playbook admin/install-incus.yml --limit nv3 --tags build-images

# force-rebuild even if the alias exists (e.g. after a version bump)
ansible-playbook admin/install-incus.yml --limit nv3 \
    --tags build-images -e force_rebuild_images=true
```

Ansible buffers command output until the task returns, so the first
`ubuntu-cuda-vllm` build (~45 min, dominated by the FlashInfer AOT
compile) shows nothing until completion. For progress, run the helper
by hand the first time:

```bash
ssh nv3.yc sudo incus-build-vllm-image           # or --force to rebuild
```

### virtiofs for host directory shares

`install-incus.yml` installs `virtiofsd`. Without it, Incus disk devices
that share a host directory into a VM silently fall back to **9p**,
markedly slower for large sequential reads (model loading).

### VM `data` passthrough on NFSv4

Incus VM `data` disks (the persistent per-VM disk attached via virtiofs)
ride the NAS VM-data subtree over **NFSv4**, not the shared CIFS `/near1`.
The CIFS + netfs writeback path deadlocks a VM's `virtiofsd` worker in
D-state (see `todo/incus/vm-data-cifs-to-nfs.md` and the private incident),
so the durable fix is moving this protocol to NFS, which has no
`cifs_reopen_file(can_flush=true)` read path.

- **Server (unconditional):** `setup-nas.yml` installs `nfs-kernel-server` on
  the NAS host and always exports the whole `/near1/vm` subtree as one NFSv4
  export, single-UID squashed to uid/gid 1000 (`all_squash,anonuid=1000,
  anongid=1000`) to mirror the CIFS `uid=1000,gid=1000` arrangement. The shared
  `/near1` CIFS export stays for its existing non-VM consumers.
- **Client (rollout switch):** the client mount is gated by a per-host Ansible
  flag, `vm_data_nfs` (default `false`; set `true` in `host_vars/<host>.yml`).
  When set, `mount-nas-share.yml` installs `nfs-common`, writes a static
  `vm-data.mount` systemd unit (`TimeoutSec=60`, no automount), enables/starts
  it, and fails if the mount is not actually present.
- **Mount check (for scripts):** scripts that touch VM `data` paths run
  `/usr/local/bin/check-vm-data-nfs-mount` (installed by the play) to fail fast
  if the mount is missing instead of writing into an empty directory.
- **Per-VM shape:** the single `/near1/vm` mount is the passthrough source;
  each VM's `data` disk is a bind mount beneath it (same shape as the old CIFS
  path), not one NFS mount per VM.
- **Verify `/data` as `ubuntu`, never root:** when checking a VM's `data`
  disk, run as the guest's login user — `incus exec <vm> -- su - ubuntu -c
  'ls -la /data'` — never a bare `incus exec <vm> -- ...`, which runs as root.
  On the virtiofs-over-NFSv4 share, root (uid 0) gets `Operation not supported`
  (ENOTSUP) for every read/write/readdir of `/data`. That is normal, not a
  broken mount: the same operations succeed as `ubuntu`. This has sent several
  debugging sessions down the wrong path (it looks exactly like a failed
  passthrough disk), so always test as `ubuntu`.
- **Do not use `raw.idmap` for uid mapping on these VMs:** setting
  `raw.idmap` (e.g. `both 1000 1000`) makes Incus launch virtiofsd with
  `--translate-uid=/--translate-gid=` instead of `--posix-acl`, and on this
  Incus/virtiofsd/QEMU stack the start wedge/fails with `Failed to add the
  virtiofs device: Monitor is disconnected`, leaving the instance STOPPED
  with a non-cancelable "Starting instance" task. The 1:1 mapping that the
  `data` passthrough already needs is instead provided by the device's
  `shift: "true"`, so leave `raw.idmap` unset.

### HF model cache (read-only share)

When a host Hugging Face cache is shared into a VM read-only, point only
the hub cache at it — `HF_HUB_CACHE=/<mount>/hub` — and leave `HF_HOME`
at its writable default. `--trust-remote-code` makes transformers write
the model's dynamic module under `$HF_HOME/modules`, which fails on a
read-only mount.

## Networking

VMs sit on `incusbr0` (`10.100.0.0/24`, NAT). The `gpu-vm-isolation`
ACL lets a VM reach the internet and the bridge subnet but **blocks all
of `10.0.0.0/8`** — VMs cannot reach the cluster or other private
ranges. The bridge resolver forwards DNS to the cluster gateway
(`10.0.0.254`).

## External SSH access

A `bastion` container on `incusbr0` runs a jump-only sshd plus an frpc
client. Each VM host gets its own public port on the frp frontend
(nv2 → `2212`, nv3 → `2213`). Users reach their VM with:

```bash
ssh -J jump@<frontend>:<port> ubuntu@<vm-name>
```

Each host's service name is set via `vm_bastion_service` in
`host_vars/<host>.yml`; its public port is derived from the node type and
index (see [Port derivation](#port-derivation)) and applied in
`admin/install-vm-bastion.yml`. `app/install-frp-server.yml` (run against
the `frontend` group) must have opened that port in the frontend `ufw` —
without it, external SSH through the bastion is silently dropped. The
original low-index hosts keep their historical ports (idx 1-3: nvidia
`2210+idx`, compute `2213+idx`, i.e. 2211-2216), so deployed bastions are
never renumbered. Every host with idx > 3 lands in a per-type disjoint
block (nvidia `2220+idx`, compute `2240+idx`), so adding a node of one
type never shifts another type's ports.

The jump user's `authorized_keys` is generated by `vm bastion-sync`:
each key line carries `permitopen` locked to exactly the VMs that user
can access (owned, or an additional user on), so a user can only tunnel
to their own VMs and any shared with them. The bastion is itself
on the isolated bridge — it cannot reach the host sshd or the cluster.
Admin access is `incus exec` on the host directly (no admin SSH key).

Deployed by `admin/install-vm-bastion.yml`.

## Design notes / open questions

These are decided directions not yet implemented — recorded so the
rationale is not lost.

### Moving vLLM into a VM

Today the host runs vLLM on its non-passthrough GPUs. Moving it into a
VM would let **all** GPUs be `vfio-pci` and remove the nvidia driver
(and the whole `nvidia-deferred` / boot-ordering mechanism) from the
host.

**Benchmarked** (MiniMax-M2.7-NVFP4, TP=2, `vllm bench serve`): a GPU
VM is within run-to-run noise of bare metal — no measurable penalty.
`NCCL_P2P_DISABLE=1` is already set (these workstation cards have no
NVLink), so GPU↔GPU traffic goes via host RAM either way, which a VFIO
VM handles at native speed. So the move is **not gated by performance**
— decide it on the isolation/simplicity merits (see `inference-vm`).

### `inference-vm` profile

A vLLM VM is **not** trusted infrastructure to be placed on the cluster
network: vLLM has a huge dependency tree and `go-vllm-m27` runs with
`--trust-remote-code`, so model-repo Python executes in-process. It
should stay **isolated and listen-only**:

- isolated bridge, egress **fully denied** (the model arrives via a
  virtiofs cache mount, not a download; set `HF_HUB_OFFLINE=1`)
- exposed to the cluster AI proxy via an Incus `proxy` device — the host
  listens on its cluster IP and forwards inbound to the VM; the VM never
  initiates a connection

This is a third profile alongside `gpu-vm` (user VMs, internet allowed).

### NAS-backed persistent user storage

Isolated VMs cannot mount cluster SMB/NFS themselves. Plan: the host
mounts the NAS CIFS export once and `vm launch` attaches a per-owner
virtiofs `disk` device (`/mnt/nas/vm-users/<owner>/` → `/data` in the
guest, read-write). Data then survives VM destroy/recreate and follows
the user across their VMs. Mount the CIFS share with `uid=1000,gid=1000`
(every guest's login user is uid 1000); use a systemd automount so a NAS
blip does not wedge `virtiofsd`.

### Pre-warm the FlashInfer kernel cache

vLLM's first start on a fresh VM JIT-compiles FlashInfer's CUTLASS MoE
kernels — measured at ~10 min on the first run; a second start in the
same VM (cache hit) takes **27 s** of init engine vs **620 s** cold, a
~23× speedup. The kernels are keyed by GPU arch (`sm120`) and
quantization (NVFP4/FP8/BF16), **not** the specific model, so they are
reusable across models.

**`flashinfer.aot` validated end-to-end** (FlashInfer 0.6.8.post1,
CUDA 13.0, `compute_120f`, MiniMax-M2.7-NVFP4 TP=2):

| Scenario | init engine | of which `torch.compile` |
|---|---|---|
| Cold (no caches) | 605 s | 46 s |
| **AOT cache only** | **71 s** | 47 s |
| Both caches hot   | 27 s | 0 s |

The AOT cache alone eliminates the FlashInfer kernel JIT (~555 s); the
remaining ~47 s is vLLM's inductor `torch.compile`, which is
model-specific and not what `flashinfer.aot` covers. An 8.5× speedup
without per-model dependencies — good enough.

`incus-build-vllm-image` does this automatically. The non-obvious bits:

- The AOT compiler needs the full flashinfer source tree (`csrc/`,
  `include/`, `3rdparty/cutlass`, `3rdparty/spdlog`), not just the
  wheel — so the script clones the matching release tag in the
  builder VM. The pin (`flashinfer_version` in `install-incus.yml`)
  must equal `flashinfer.__version__` in the wheel; mismatch silently
  bypasses the AOT cache at runtime, so the script verifies it.
- A small wrapper drives `flashinfer.aot.compile_and_package_modules`
  from the *installed wheel* (so its cubin / version checks pass) but
  with `project_root=<clone>` (so include paths point at the source).
- `FLASHINFER_CUDA_ARCH_LIST=12.0` is auto-normalised to
  `compute_120f` under CUDA ≥ 12.9; the SM120 module guards
  substring-match `compute_120`, so this still enables them.
- `MAX_JOBS=8` on a 48 GiB builder. Each `cicc` peaks above 5 GiB RSS,
  so the script provisions ~8 × 5 GiB plus headroom. At 32 GiB the
  default 8-wide saturates and OOM-kills the in-VM agent (verified);
  at 32 GiB you would need to drop to `MAX_JOBS=4` and accept ~70 min
  instead of ~40.
- Output (~940 MiB) goes straight into `<wheel>/data/aot/`, which is
  exactly where `JitSpec.aot_path` looks when the optional
  `flashinfer-jit-cache` package is not installed (it isn't — PyPI
  has it quarantined). No runtime env override needed.

The SM120 NVFP4 MoE kernels we actually exercise are `fused_moe_120`,
`fp4_quantization_120(f)`, `gemm_sm120`, `fp4_gemm_cutlass_sm120`,
`mxfp8_gemm_cutlass_sm120` — all built by `--add-moe true`. The script
builds the full default set (`--add-comm/gemma/oai-oss/moe/act/misc/xqa`
all true, plus attention which is unconditional) so non-MoE / non-NVFP4
models also benefit; trim to `--add-moe true` alone if image size matters
more than broad coverage.

vLLM's `torch.compile` cache, by contrast, is model-specific — do not
bake it; keep it in a persistent per-VM volume instead.
