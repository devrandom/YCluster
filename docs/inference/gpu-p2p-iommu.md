# GPU PCIe P2P: host IOMMU fix vs. 0xSero recipe

Status: **investigating** — no host change applied. Prefer a recipe that keeps
IOMMU/VFIO; use the host PCIe fix only as a fallback.

## Why this exists

A public 4× RTX PRO 6000 Max-Q report moved C1 codegen decode from **237 to
444 tok/s** after a host-only change: `amd_iommu=off`, the NVIDIA P2P registry
keys, and one reboot (461 tok/s with the exact flags, 414 best verified config).
A separate public report reaches **670 tok/s** C1 codegen with a custom
codegen-tuned B12X kernel that is not published yet. The linked Flare runtime is
a different serving stack, not the same vLLM recipe.

Our DS4.1 C1 codegen baseline is **208–250 tok/s**. The host is in the public
report's "before" state:

- IOMMU on (53 groups), no `amd_iommu=off`.
- `/etc/modprobe.d/nvidia-p2p-override.conf` absent.
- `RegistryDwords: ""`, `EnableResizableBar: 0`.
- `nvidia-smi topo -p2p` reports OK, but ACS is enabled on every GPU root port.

The P2P capability exists, so this is a bandwidth/path problem, not a missing
feature.

The public R36–R38 images do not close the 670 gap: R36's SSD C1 median is
244 tok/s, R37 is ~259 at budget 75 / 281 at budget 50, and R38 is ~257. R37
implements SSD Engram but does not claim a repeated SSD benchmark; R38's public
deployment uses RAM Engram. The 670 number needs the unpublished kernel, not a
newer R-tag.

## Checking P2P capability and bandwidth

Run these from a host or guest where the NVIDIA driver owns the GPUs:

```bash
nvidia-smi topo -m
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
nvidia-smi --query-gpu=index,pci.bus_id,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current --format=csv
```

`topo -p2p` is a capability matrix, not a bandwidth result. `OK` means the
driver permits peer access; `NS`, `TNS`, `CNS`, and `GNS` mean the topology,
chipset, or GPU does not support it; `DR` means a registry key disabled it.
`topo -m` gives the path: `PIX`/`PXB` are direct through PCIe switches, while
`PHB`, `NODE`, and `SYS` traverse host bridges. P2P can be `OK` through a host
bridge, but that is not the same path as a direct switch hop.

Validate the capability with a real transfer:

```bash
nvidia-smi -L
nvbandwidth -p device_to_device -b 512
# or the CUDA sample:
p2pBandwidthLatencyTest
```

`nvbandwidth` was not available in the test environment. The measurements
below used an equivalent CUDA runtime loop: `cudaDeviceEnablePeerAccess()`
followed by `cudaMemcpyPeer` with 64 MiB buffers and 3 repetitions (256 MiB and
5 repetitions on the 3-GPU test). Every tested pair returned success from
`cudaDeviceEnablePeerAccess()`.

If `nvidia-smi` fails with `couldn't communicate with the NVIDIA driver`, the
GPUs may be bound to `vfio-pci`. Check the host and any multi-GPU guests:

```bash
lspci -nnk -d 10de:
incus list
incus config show <vm> | grep -A4 gpu
incus exec <vm> -- nvidia-smi topo -p2p r
```

A guest needs at least two GPUs to test a pair. A host that assigns one GPU to
each VM cannot measure P2P without a maintenance change.

If the GPUs are host-bound, record the host-side path too:

```bash
cat /proc/cmdline
ls /sys/kernel/iommu_groups
lspci -vv -s "$BDF" | grep -E 'ACSCtl:|LnkCap:|LnkSta:'
grep -E 'EnableResizableBar|RegistryDwords' /proc/driver/nvidia/params
```

ACS redirect/upstream-forwarding does not necessarily block peer access, but it
forces traffic through the host bridge. Do not disable ACS or IOMMU just to
change the reported path; that costs VFIO isolation.

### Measured results

| Host profile | GPUs | Capability | Path / link | Measured one-way BW |
|---|---:|---|---|---|
| 4-GPU Gen5 server, one GPU per VM | 4 | not measurable at host | Gen5 x16; separate VMs | not measured |
| 3-GPU Gen5 host | 3 | all pairs `OK` r/w | `NODE`, Gen5 x16 | 53.0–55.2 GB/s |
| 4-GPU Gen5 all-VFIO host | 4 (3 tested) | tested pairs `OK` r/w | `PHB`, Gen5 x16 | 52.9–54.5 GB/s |
| 6-GPU Gen5 host | 6 | all 30 ordered pairs `OK` r/w | `PHB`/`NODE`; one card Gen5 x8 | 53.2–55.3 GB/s on x16 pairs; 27.0–28.4 GB/s on pairs involving the x8 card |
| 4-GPU Gen4 host | 4 | all pairs `OK` r/w | `NODE`, Gen4 x16 | 26.6–28.0 GB/s |

Results are per direction. The Gen4 host is limited by its PCIe generation,
not by a P2P failure. On the 6-GPU host, the x8 card caps every pair that
includes it; a 64 MiB GPU 3→5 sample measured 40.9 GB/s, but a 256 MiB retest
measured 52.7 GB/s, so the lower sample was treated as contention noise. The
all-VFIO host has one GPU isolated in a single-GPU guest, so only three of its
four GPUs were tested.

The exact host-to-profile mapping is deployment-specific and kept in the
private deployment notes, not in this public doc.

## Options

| Lead | What it changes | Fit |
|---|---|---|
| 0xSero DS4.1 4× RTX PRO 6000 recipe | serving recipe, no IOMMU change | **preferred**; track it |
| Host PCIe fix | `amd_iommu=off` + NVreg P2P keys + ReBAR + reboot | fallback; costs VFIO |
| Codegen-tuned B12X kernel | custom kernel from the 670 tok/s report | not published; watch |
| Flare runtime | separate Rust runtime | not drop-in; needs 188.8 GiB tables (≥256 GiB host RAM) or ~515 GB disk |

### Preferred: 0xSero recipe

Benchmark 0xSero's DS4.1 4× RTX PRO 6000 recipe first. It may get the P2P
gain without changing IOMMU, so it keeps VFIO and the current host split.
Run it against the same C1 codegen and conc 8/32 baselines before any host
change.

### Fallback: host PCIe fix

If the recipe does not get the gain and the host fix is approved, apply it on the
all-passthrough four-GPU host, not the current DS4.1 host. That host has all four
cards in the VFIO pool; the Comfy VM holds one, so this path retires or moves
that VM. The current host keeps its two passthrough cards.

The host fix is:

```text
amd_iommu=off
options nvidia NVreg_RegistryDwords="ForceP2P=0x11;RMForceP2PType=1;RMPcieP2PType=2;GrdmaPciTopoCheckOverride=1;EnableResizableBar=1"
update-grub
reboot
```

### Stacking

If the 0xSero recipe and the host fix both help, measure the combination
(recipe × P2P, 2×2) rather than assuming the gains add. They may remove the
same bottleneck.

## Decision

Keep the current host's VFIO split. Do not apply the host fix there. Wait for
0xSero's recipe; if it does not get the gain and IOMMU off is required, move the
deployment to the all-passthrough four-GPU host. Re-evaluate stacking only after
both paths are measured independently.

## Caveats

- The active four-GPU set is all `NODE/PHB`, and one active card is PCIe x8;
  the public 444 box is all x16. Do not promise 444.
- Ubuntu's generic kernel has no `pcie_acs_override`, and the root ports have
  ACS enabled. The NVreg keys may not be enough without a BIOS ACS change.
- Disabling IOMMU drops DMA isolation and VFIO GPU passthrough. Never apply it
  to the current DS4.1 host if that host's passthrough cards are still needed.
- On the current host the DS4.1 cards are host-visible; the two passthrough
  cards belong to a stopped VM. The host fix would not stop DS4.1, but it would
  break that VM's GPU path. On the all-passthrough host the Comfy VM holds one of
  the four cards, so the fallback retires or moves it.
- The 670 tok/s report is a custom kernel, not a config knob. The 444 tok/s
  report is a host P2P result. Keep them separate.

## Acceptance

- Use the pinned `llm-inference-bench` client from the R36 receipt
  (`80d1f1b0ab9830c3fd8a22c42f461c40cbc7cf96`) so the numbers stay
  comparable.
- C1 codegen: `Sieve of Eratosthenes`, 512 output tokens, temperature 1,
  top-p 0.95, `ignore_eos`.
- Conc 8/32: aggregate decode, per-request decode, TTFT.
- Success: material C1 gain; per-request decode not collapsing; TTFT not
  exploding at the new ceiling.
- After a host change: `cat /proc/cmdline`, `ls /sys/kernel/iommu_groups`,
  `grep -E 'EnableResizableBar|RegistryDwords' /proc/driver/nvidia/params`,
  `nvidia-smi topo -p2p r/w`.
- Revert if no gain.

## References

- Public Max-Q host P2P report:
  <https://x.com/Dataphil3/status/2113031325884002597>
- Public codegen kernel report:
  <https://x.com/fraserpricee/status/2112924981525135749>
- Flare package:
  <https://huggingface.co/local-inference-lab/DeepSeek-V4.1-Flash-1017-FLARE>
- 0xSero recipes: <https://github.com/0xSero>
