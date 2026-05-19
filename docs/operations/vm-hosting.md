# GPU VM Hosting

YCluster runs GPU-passthrough virtual machines on designated Nvidia
nodes via [Incus](https://linuxcontainers.org/incus/). VMs get one or
more whole GPUs by PCI passthrough, an isolated NAT network, and
optional external SSH access. The `ycluster vm` CLI manages them.

- **Hosts**: any node with `incus_vm_host: true` in its host_vars
  (currently `nv2`, `nv3`).
- **Playbook**: `admin/install-incus.yml` — ends early on non-VM-hosts.
- **Image**: `ubuntu-cuda` — Ubuntu 24.04 + NVIDIA open driver + CUDA,
  built on the host by `incus-build-gpu-image`.

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
   `driver_override` and `drivers_probe` it. This is **address-specific**.
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
- `/cluster/vms/<name>` — `{"owner": ..., "gpus": N, "created": ...}`

### Disk resize

`ycluster vm resize` overrides the profile root-disk size, then restarts
the VM (Incus only applies a new size on start). cloud-init grows the
partition and filesystem on boot. Grow-only — Incus cannot shrink.

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
host. GPU compute in a VFIO VM is near-native; for tensor-parallel
inference the only risk is GPU↔GPU traffic, and the launch scripts
already set `NCCL_P2P_DISABLE=1` (these workstation cards have no
NVLink), so that traffic goes via host RAM regardless — a VM handles it
fine. Pending a TP=2 benchmark, host vs. VM.

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
