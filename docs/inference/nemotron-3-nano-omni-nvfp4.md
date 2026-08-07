# Nemotron-3 Nano Omni 30B-A3B (NVFP4) on RTX PRO 6000 / SM120

Runtime: stock `vllm-openai` v0.20.0 (CUDA 13.0) from the NVFP4 model card,
serving NVIDIA's first-party ModelOpt NVFP4 quant of NVIDIA's own
Nemotron-3 Nano Omni BF16 checkpoint. This is the same Docker Hub image
family as the MiniMax-M3 stock stack, so no new registry/auth requirement.
Alternative official image if you prefer NGC: `nvcr.io/nvidia/vllm:26.05-py3`
(CUDA 13.2.1, vLLM 0.20.1, explicitly lists Nemotron-3 Nano Omni support).

## Why this image

- vLLM **0.20.x** is the first with native support for the Nemotron-3 Nano
  Omni Qwen2VL-family architecture (NGC 26.04 / vLLM 0.19 needs an
  `--hf-overrides` architectures override — avoid it).
- **NVFP4 is Blackwell-only** (RTX PRO 6000 / sm_120 is supported hardware).

## Checkpoint

- Repo: `nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4`
- Revision: pinned to `main` tip SHA `dc5f0b0bfddf8b6e0f5891475be9af05b80126fe`
  (no stable tag exists; re-verify the SHA at download time).
- ~22 GB on disk (~21 GB NVFP4 weights). Official first-party NVIDIA quant,
  not gated, NVIDIA Open Model Agreement.
- Precision family: BF16 (62 GB) / FP8 (33 GB) / NVFP4 (21 GB).

## GPU / VRAM sizing

- Native context window is 256K; we serve at 128K (`--max-model-len 131072`)
  on a single 96 GB card.
- **KV cache is not the memory driver.** This is a hybrid model: only **6 of the
  52 layers are attention** (`*` in `hybrid_override_pattern` = attention,
  `M` = Mamba-2, `E` = MoE/MLP-only). FP8 attention KV is
  `2 (K+V) × 2 KV heads × 128 head_dim × 6 layers × 1 B = 3,072 B/token`:
  ~**0.38 GiB per 128K sequence**, ~0.09 GiB at 32K. Even with all
  `--max-num-seqs 32` slots full at 128K the worst case is ~12 GiB. Mamba
  state is fixed-size and sequence-length independent (~0.72 GiB for 32 seqs).
  So "26-29 GiB of KV" reported in vLLM logs is a **memory-fill pool** — the
  leftover of `gpu_memory_utilization × card` after the model stack (NVFP4 LLM
  + large omni projector + vision/audio encoders + CUDA graphs) loads — not
  what a 128K sequence actually needs.
- `--gpu-memory-utilization 0.80` (~76 GB budget) deliberately leaves the
  allocator more than the model needs; this is what surfaces as the large KV
  pool. Lower it (0.45-0.50) to co-tenant on a shared card; the inherent KV
  need is far below that. Set it explicitly — vLLM otherwise defaults to
  aggressive allocation.
- TP=1. NVFP4-quantized MoE rejects `--moe-backend triton` ("not supported for
  NvFP4 MoE"); the supported backends are `cutlass`, `flashinfer_*`, and
  `marlin`. Use `flashinfer_cutlass` (the model-card default for this quant).

## Co-tenancy with MinerU on one host

MinerU and Nemotron share the same host-visible GPU (CDI index 0) on nv2.
MinerU keeps its own pin (`mineru-podman.service` → `--device
nvidia.com/gpu=0`) but its vLLM budget is now lowered from the 0.5
auto-default to `--gpu-memory-utilization 0.2 --max-model-len 8192`, so
instead of holding ~48 GB of the 96 GB card it holds ~20 GB idle. Nemotron
is pinned to the same card (`nemotron_gpus: "0"`) with a **lowered
`gpu-memory-utilization` (0.45, ~43 GB budget)**: ~21 GB NVFP4 weights + a
~15-20 GB FP8 KV pool — far above the real need (~0.38 GiB per 128K seq,
~12 GiB worst case with all 32 slots full). GPU 1 stays completely
unrestricted. The two never collide because each container is pinned to one
CDI GPU and both are capped underneath the card total — do not fall back to
`gpu=all` for either while they share the host. Verify with `nvidia-smi`
(summed usage < 96 GB) after a live parse + Nemotron prompt. See
Admin-deployment below for the host_var opt-in.

Measured (nv2, vLLM 0.20.0, `--gpu-memory-utilization 0.45`,
`--moe-backend flashinfer_cutlass`): Nemotron model stack ~21.5 GiB, KV pool
~16.3 GiB, Nemotron EngineCore ~47.2 GiB. MinerU EngineCore with the 0.2
budget is ~19.8 GiB idle (was ~48 GiB at the 0.5 default). The card sits at
~68 / 97.9 GiB (**~29 GiB free headroom**), so there is real room to raise
Nemotron's KV pool or `--max-num-seqs` if needed without touching MinerU.
GPU 1 stays idle.

## Launch (vLLM — text / image / video; audio caveat below)

```
vllm serve /models/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4 \
  --served-model-name nemotron-3-nano-omni \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --max-model-len 131072 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.45 \
  --max-num-seqs 32 \
  --moe-backend flashinfer_cutlass \
  --reasoning-parser nemotron_v3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --limit-mm-per-prompt '{"image":8,"video":1,"audio":1}' \
  --host 0.0.0.0 --port 9217
```

The per-prompt counts are **cumulative over the whole conversation**, not per
user turn — Open-WebUI re-sends every prior image into each new message, so an
`image` cap of 1 rejected any second image across turns ("at most 1 image(s)
may be provided in one prompt"). The NVFP4 model itself supports unlimited
images, so this is just our own knob. `image:8` is a middle ground for
multi-turn image chat; vLLM reserves multimodal-prefill memory roughly in
proportion to the limit, so keep it low on the shared card (raise via
`MM_IMAGE_LIMIT` in `~dev/bin/nemotron-start.sh` if a longer image history is
routine). Video/audio stay at 1.

**Audio**: decoding audio input needs the `vllm[audio]` extra that is *not* in
the base image. Text / image / video work out of the box; audio is the open
follow-up (layer `vllm[audio]` into a wrapper image following the MinerU build
pattern).

## Admin deployment

`config/ansible/admin/install-nemotron-3-nano-omni.yml` templates two dev
scripts (`~/bin/nemotron-setup.sh`, `~/bin/nemotron-start.sh`) and, on the
storage leader, registers the backend with `ycluster inference add
http://<host>.xc:9217 nemotron-3-nano-omni`. Per-model opt-in: set
`nemotron_enabled: true` and `nemotron_gpus: "1"` in host_vars (same pattern as
`ds4_enabled` / `mineru_enabled`). Then:

```
ssh <host>.yc
~/bin/nemotron-setup.sh --download   # pull image + CDI smoke + ~22 GB checkpoint
~/bin/nemotron-setup.sh              # later runs: just image pull + smoke
~/bin/nemotron-start.sh              # launch (idempotent)
```

## Verification

- `curl -fsS http://127.0.0.1:9217/v1/models` lists `nemotron-3-nano-omni`.
- Through the gateway: `https://inference.xc/v1/chat/completions` with
  `"model": "nemotron-3-nano-omni"` — a reasoning request should stream
  `reasoning_content`, and an image request should actually see the image.
- `nvidia-smi` on the host: MinerU and Nemotron both live on GPU 0 (summed
  usage below 96 GB, currently ~20 MinerU + ~47 Nemotron with ~29 GB free);
  GPU 1 stays unrestricted. Both can
  warm/cold-start without OOM given the summed budget is under the card.
