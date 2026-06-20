# MiniMax-M3 NVFP4 on 4× RTX PRO 6000 Blackwell (SM120)

> **Superseded — see [minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md) for the verified working runbook.**
> This document's core premise was wrong: the SGLang/0xSero cutlass NVFP4 path
> does **not** work on SM120 (it serves NaN/garbage; the upstream "validation"
> only checked for a 200, not coherent output). The working path is the **b12x**
> kernel image. Kept here for the investigation/background only.

Deployment runbook. Single-reporter validation — treat the operating envelope as a starting point, not gospel.

## TL;DR

M3 on 4× RTX PRO 6000 is **viable today via SGLang on a pinned lmsys dev image** — not via the stable vLLM TP=8 stack, and not via vLLM at all yet. It slots in as a parallel evaluation lane alongside the Lisbon vLLM cluster, not a replacement. Consistent with the SM120-lag rule: support exists ~2–3 weeks after release, but only on the bleeding-edge serving path.

The working recipe is **[0xSero/minimax-m3-nvfp4-sglang](https://github.com/0xSero/minimax-m3-nvfp4-sglang)** — README states it is validated on 4× RTX PRO 6000 Blackwell. Nine files: a Dockerfile `FROM` a digest-pinned lmsys image plus two trivial Python text-patches.

## Model facts

MiniMax-M3: 428B total / 23B active multimodal MoE, MiniMax Sparse Attention (MSA), up to 1M-token context. 854 GB at BF16, ~444 GB MXFP8, ~240–260 GB NVFP4. Sampling: temperature=1.0, top_p=0.95, top_k=40. Native VL architecture `minimax_m3_vl` / `MiniMaxM3SparseForConditionalGeneration`.

NVIDIA has **not** published an official `nvidia/MiniMax-M3-NVFP4` yet (org tops out at M2.7-NVFP4). All M3 NVFP4 quants are community, ~5–7 days old.

## NVFP4 checkpoint options

| Repo | Size | Notes |
|------|------|-------|
| `brandonmusic/MiniMax-M3-NVFP4` | 241 GB | Routed experts fully NVFP4; only dense/shared fused `gate_up_proj` left BF16 (calibration-bug scar, documented honestly). Full VL stack preserved. Ships `merged_amax_gatefix.safetensors` → re-export in ~25 min. Best-documented. |
| `lukealonso/MiniMax-M3-NVFP4` | 246B params | Most-downloaded (~12k). Author of the b12x SM120 kernel stack + quant-toolkit. |
| `bullerwins/MiniMax-M3-NVFP4` | — | Repack of lukealonso's to load directly on the 0xSero sglang fork. |
| `sparkarena/Minimax-M3-v0-NVFP4` | — | DGX Spark focus, explicitly not Spark-only; RTX Pro 6000 path via custom sglang container. REAP50 variant fits 2× nodes. |
| `Mapika/MiniMax-M3-NVFP4` | 256 GB | Experts-only NVFP4 (nvidia M2.7 recipe family), ModelOpt 0.44.0. |

**Checkpoint selection test (do this before downloading 240 GB):** read the candidate's `model.safetensors.index.json` weight-map keys and check for a leading `model.` prefix. The 0xSero loader patch strips that prefix; a checkpoint exported through the transformers native `minimax_m3_vl` path won't have it (the conversion moves the `language_model` prefix and renames keys), while a raw/older export will. Either way the patch is idempotent and safe — but this tells you whether it's load-critical or a no-op for your chosen drop. The real risk is a key-layout mismatch the patch doesn't cover, which `make verify` surfaces in one request.

## Architecture: where SM120 MSA support actually lives

- The **MSA sparse-attention kernels, the `minimax_m3_vl` model class, and the swigluoai-correct fused MoE are compiled into the lmsys base image** (`lmsysorg/sglang:dev-cu13-minimax-m3`, CUDA 13), pinned by digest `sha256:a081e28f8d6368de246ae66459efbc6f94ea20ffa15fe3f3ce6b0820e9b58eea`.
- lukealonso's b12x SM120 work was upstreamed into that lmsys dev build. That's why **public b12x has no M3/MSA branch** (branches are GLM-5.1 / Kimi / `dev/sparse_mla_unified` etc.). You are *consuming* a pre-built MSA-SM120 kernel, not porting one.
- The 0xSero repo's entire delta on top of the lmsys image is two text-patches (below).

### The two patches (full delta)

**`patch_loader.py`** — one-line insertion into the baked-in `minimax_m3_vl.py`: strip a leading `model.` prefix from weight names before dispatch, so text `self_attn` q/k/v doesn't get misrouted into the ViT QKV merge path. Idempotent (checks before applying); anchored on an exact source string; **fails closed** if the anchor isn't found (good — a future base-image change breaks the build loudly instead of silently).

**`patch_model_config.py`** — rewrites the mounted `config.json`:
- renames `minimax_m3_sparse` layer labels → `sparse`
- backfills `vision_config.rope_theta` from `rope_parameters`
- synthesizes the full `sparse_attention_config` block (freq mask, index heads, block size, topk, local blocks) from the M3 `index_*` fields — without this the loader can't construct the MSA layer config

Runs on the host against the mount source **before** launch (weights mount `:ro`; only the small `config.json` is mutated).

## Validated operating envelope

Single-reporter, 4× RTX PRO 6000. Starting point, tune from here.

```
tensor parallel:        4
quantization:           modelopt_fp4
MoE backend:            flashinfer_cutlass
KV cache dtype:         bfloat16
context length:         450000
max total tokens:       450000
decode CUDA graph:      full
prefill CUDA graph:     disabled   # prefill graph backends crash: idx_q mismatch in unified_attention_with_output
max running requests:   4
chunked prefill size:   4096
max prefill tokens:     16384
mem-fraction-static:    0.95
power cap:              275W per GPU (external to the repo)
served model id:        minimax-m3
port:                   8000
```

**Hard constraints:**
- Prefill CUDA graph **must stay disabled**. Do **not** add `--disable-cuda-graphs` or `--enforce-eager`.
- `max-running-requests 4` is the binding concurrency ceiling for 5–10 agentic users on this 4-GPU node — and it's the real limiter, not VRAM.
- `mem-fraction-static 0.95` at 450K context is aggressive on 4×96 GB; little OOM margin. First tuning knob is **dropping context length**, not the static fraction.
- Prefill graph disabled → prefill-heavy agentic bursts are the latency risk, not decode.

## Deployment sequence (priority order)

### 1. Decouple from the pinned dev image — do this first
Highest-leverage action. Everything depends on `lmsysorg/sglang@sha256:a081e28f…` and **dev tags get garbage-collected**. Pull it now, `docker save` / skopeo-copy into your own registry, pin from there. (Same stale-perishable-artifact logic as the aggregator-price rule — the image is the scarce thing here, not the 9-file repo.) Confirm the digest still resolves before planning around it.

### 2. Mirror weights through the German-entity flow → Ceph/NAS
Pick the checkpoint by the index-key test above. Point `MODEL_DIR` at the local copy.

### 3. Translate to rootless Podman + Quadlet (your standard, not rootful Docker)
Mechanical mapping from the compose file:

| Compose | Quadlet / Podman |
|---------|------------------|
| `network_mode: host` | `Network=host` |
| `ipc: host` | `PodmanArgs=--ipc=host` |
| `gpus: all` | CDI `Device=nvidia.com/gpu=all` (or existing GPU Quadlet pattern) |
| `ulimits: memlock -1` | `Ulimit=memlock=-1:-1` |
| `ulimits: stack` | `Ulimit=stack=67108864:67108864` |
| `PYTORCH_CUDA_ALLOC_CONF` | `Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |

- Likely also need `--security-opt seccomp=unconfined` and the `keep-groups` pattern used for other GPU containers.
- Run `patch_model_config.py` as `ExecStartPre=` on the host against a **writable copy** of `config.json` (weights stay `:ro`).
- Bake `patch_loader.py` into your own image build on top of the mirrored base. Keep its fail-closed anchor check.

### 4. Gate production on two cheap numeric checks
- **swigluoai integrity** — M3 experts use GPT-OSS-style clamped SwiGLU: `(clamp(up,±7)+1)·clamp(gate,max=7)·σ(1.702·gate)`, gate-then-up halves, **not interleaved**. A kernel that silently falls back to plain SiLU·up degrades all 57 MoE layers. Send a temp-0 prompt and compare against the MiniMax API reference for the same input; sharp divergence = fallback. `flashinfer_cutlass` on the lmsys image is **unverified** for this — probe it.
- **concurrency ceiling** — ramp concurrency, watch the prefill path (graph disabled). Confirm `max-running-requests 4` headroom before pointing real users at it.

## Quick start (reference; adapt to Podman per above)

```bash
git clone https://github.com/0xSero/minimax-m3-nvfp4-sglang.git
cd minimax-m3-nvfp4-sglang
# weights expected at /mnt/llm_models/MiniMax-M3-NVFP4
make run
make verify
```

`verify` hits `/v1/models` then sends a one-word chat completion at temperature 0.

## Open items (all empirical — resolve in one afternoon with the node freed up)
1. Which exact NVFP4 checkpoint 0xSero validated against (no provenance in the repo's 2 commits; loader patch implies a `model.`-prefixed drop).
2. Whether `flashinfer_cutlass` on the lmsys image honors swigluoai or hits the silent-SiLU fallback.
3. Real concurrency headroom beyond the stated `max-running-requests 4`.

## Source map
- Recipe / gotchas: [brandonmmusic-max/minimax-m3-nvfp4-recipe](https://github.com/brandonmmusic-max/minimax-m3-nvfp4-recipe)
- Serving fork: [0xSero/minimax-m3-nvfp4-sglang](https://github.com/0xSero/minimax-m3-nvfp4-sglang)
- Kernel stack / quant toolkit: lukealonso/b12x, local-inference-lab/quant-toolkit
- Base image: `lmsysorg/sglang@sha256:a081e28f8d6368de246ae66459efbc6f94ea20ffa15fe3f3ce6b0820e9b58eea`

## Mac Studio alternative (lighter eval path)
`mlx-community/MiniMax-M3-4bit` exists, converted via **mlx-vlm 0.6.3** (multimodal path, not mlx-lm). On a 512 GB M3 Ultra: ~230–250 GB at 4-bit, comfortable with KV headroom, single node. Expect ~20–30 tok/s decode (2.3× M2.5's active params; M2.5 4-bit ran ~53 tok/s). Don't go below 4-bit (M2.5 showed 43% token accuracy at q3.5 vs 91%+ at q4.5). Open question: whether mlx-vlm preserves MSA or falls back to dense attention at long context — verify before trusting for 196K+ agentic runs. This is an eval/fallback box, not a cluster replacement.
