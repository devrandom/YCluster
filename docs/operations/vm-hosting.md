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
guest automatically and refreshed on `ssh add/remove`.

### etcd layout

- `/cluster/users/<user>` — `{"ssh_keys": [...]}`
- `/cluster/vms/<name>` — `{"owner", "gpus", "host", "created", "state"}`.
  `state` is `provisioning` while a launch is in progress and `ready`
  once it completes — so an interrupted launch is recognisable. `host`
  records which VM host the VM runs on (`vm list` shows runtime state
  only for the local host's VMs; others show `(remote)`).

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

A `bastion` container on `incusbr0` runs a jump-only sshd plus a rathole
client. The rathole server exposes the `vm-bastion` service publicly on
port `2210` (on the frontend). Users reach their VM with:

```bash
ssh -J jump@<rathole-host>:2210 ubuntu@<vm-name>
```

The jump user's `authorized_keys` is generated by `vm bastion-sync`:
each key line carries `permitopen` locked to exactly the VMs that user
owns, so a user can only tunnel to their own VMs. The bastion is itself
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
