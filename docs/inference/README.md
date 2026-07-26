# Inference Setups

Per-model / per-hardware deployment runbooks for bringing up an inference
backend that the gateway (`local-ai-proxy`, see
[../operations/inference.md](../operations/inference.md)) can then route to.

Each file documents one verified-working stack: the image/runtime, the
checkpoint, the launch command, the non-obvious flags, and how to verify it
actually produces coherent output (not just a 200). Keep them
deployment-agnostic — no node names, IPs, or account specifics.

## Setups

- [MiniMax-M3 4-bit on RTX PRO 6000 / SM120](minimax-m3-nvfp4-sm120.md) —
  current prod (2026-07): stock vLLM 0.24 + MXFP4 checkpoint + EAGLE3
  spec-decode (117–169 tok/s single-stream), with one auto-derived 3-line
  clamp patch. Stock *NVFP4* paths remain broken on SM120 (MXFP4 is the
  format that works). The retired b12x NVFP4 stack is at
  [../archive/minimax-m3-b12x-stack.md](../archive/minimax-m3-b12x-stack.md).
  Background/investigation: [minimax-m3-design-2026-06.md](minimax-m3-design-2026-06.md)
  (superseded — documents the path that turned out not to work).

## Design / remediation notes

- [Reproducible build of the b12x SM120 vLLM image](b12x-reproducible-build-design-2026-06.md)
  — plan to replace the opaque prebuilt Docker Hub image with one built from
  pinned public sources and hosted in our registry.
- [GLM-5.2 NVFP4 on RTX PRO 6000 / SM120](glm-5.2-nvfp4-sm120-design-2026-06.md)
  — full 744B `nvidia/GLM-5.2-NVFP4` on the b12x stack (same image family as M3,
  stock tool calling), serving on the whole 8-GPU node at PP=2×TP=4 (~2.2×
  prefill vs flat TP=8 on this no-NVLink box). Includes the node-crash incident
  and the retired-REAP history.
