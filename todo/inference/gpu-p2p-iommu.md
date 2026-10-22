# GPU PCIe P2P: prefer the 0xSero recipe over IOMMU off

Status: **waiting on 0xSero's DS4.1 4× RTX PRO 6000 recipe**; no host change.

The current DS4.1 host is in the public report's "before" state: IOMMU on, no
NVreg P2P registry keys, ReBAR off; C1 codegen is 208–250 tok/s. A public
Max-Q report moved 237 → 444 tok/s after `amd_iommu=off` + NVreg P2P keys +
one reboot.

Preferred: benchmark 0xSero's DS4.1 recipe first; it may get the gain without
IOMMU and keep VFIO. Fallback if IOMMU off is required: apply it on the
all-passthrough four-GPU host, not the current host, so the current host keeps
its two passthrough cards. If both help, measure the combination; do not assume
the gains add.

Acceptance: same C1 codegen and conc 8/32; material C1 gain, per-request decode
not collapsing, TTFT not exploding; verify cmdline, IOMMU groups, P2P keys, and
ReBAR; revert if no gain.

Also track Fraser's unpublished codegen-tuned B12X kernel (670 tok/s) and the
Flare runtime (not a drop-in); see the doc.

See [docs/inference/gpu-p2p-iommu.md](../../docs/inference/gpu-p2p-iommu.md).
