# Inference Setups

Per-model / per-hardware deployment runbooks for bringing up an inference
backend that the gateway (`local-ai-proxy`, see
[../operations/inference.md](../operations/inference.md)) can then route to.

Each file documents one verified-working stack: the image/runtime, the
checkpoint, the launch command, the non-obvious flags, and how to verify it
actually produces coherent output (not just a 200). Keep them
deployment-agnostic — no node names, IPs, or account specifics.

## Setups

- [MiniMax-M3 NVFP4 on RTX PRO 6000 / SM120](minimax-m3-nvfp4-sm120.md) —
  b12x kernel image; the stock cutlass NVFP4 MoE path is broken on SM120.
  Background/investigation: [minimax-m3-design-2026-06.md](minimax-m3-design-2026-06.md)
  (superseded — documents the path that turned out not to work).

## Design / remediation notes

- [Reproducible build of the b12x SM120 vLLM image](b12x-reproducible-build-design-2026-06.md)
  — plan to replace the opaque prebuilt Docker Hub image with one built from
  pinned public sources and hosted in our registry.
- [GLM-5.2 NVFP4 on RTX PRO 6000 / SM120](glm-5.2-nvfp4-sm120-design-2026-06.md)
  — plan to deploy GLM-5.2 on the same b12x stack as M3 (reuses the image
  family, gains stock tool calling); evaluate the 4-GPU REAP-469B variant
  first, then decide on full 744B at TP=8.
