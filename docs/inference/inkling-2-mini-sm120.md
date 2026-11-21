# Inkling 2 Mini on RTX PRO 6000 (SM120)

Runtime: `lmsysorg/sglang:inkling-2-mini`, pinned by digest. SGLang is the
runtime that verifies RTX PRO 6000 (SM120) at TP1/EP1 for this model;
sgl-project/sglang#51926 adds the SM120/SM121 path (looped checkpoint + MTP
draft, native NVFP4/MXFP8 grouped GEMMs, FP32 LM head GEMV). The vLLM
`inkling-2-mini` image is the fallback if this one cannot initialize.

## Checkpoint

- Repo: `thinkingmachines/Inkling-2-Mini`, Apache-2.0, not gated.
- Revision: `ed40e7da625f594fc9c4990f5f66be24873c22d7`.
- ~20.2 GB: 19.5 GB main safetensors + 0.64 GB `mtp.safetensors` (the
  bundled 3-layer DSpark draft head). Layer 0-19 experts are NVFP4, 20-23
  are MXFP8. 24 physical layers, loop core 6-17 runs three times, 128
  routed experts top-8, text + image in, 1,048,576-token native context.

## Launch

The model is served on one card at TP1/EP1. This is the verified RTX PRO
6000 Balanced cell plus the repo's `--enable-metrics`, with a conservative
context and memory fraction:

```
sglang serve --model-path /model \
  --served-model-name inkling-2-mini \
  --tp 1 --ep 1 \
  --attention-backend fa4 \
  --dtype bfloat16 \
  --page-size 128 \
  --context-length 262144 \
  --mem-fraction-static 0.65 \
  --grammar-backend xgrammar \
  --enable-multimodal \
  --enable-metrics \
  --reasoning-parser inkling \
  --tool-call-parser inkling2 \
  --host 0.0.0.0 --port 9220
```

DSpark is enabled in this deployment (`inkling_dspark: true`). It adds the
four upstream `SGLANG_OPT_*` variables (`INKLING2_VERIFY_REL_BIAS_KERNEL`,
`DSV41_DSPARK_MID_FOLD`, `DSV41_DSPARK_PREPARE_INPUTS_ONCE`,
`DSV41_DSPARK_SKIP_PREFILLED_INPUTS`) and `--speculative-algorithm DSPARK
--speculative-num-draft-tokens 8 --max-running-requests 4`. The concurrency
cap of 4 is real: size any multi-user expectation around it.

`--enable-metrics` is required for the `sglang` Prometheus job and the
`InferenceSGLangCachePressure` alert (`sglang_token_usage`).

## Sizing

Measured on a 96 GB RTX PRO 6000 co-tenanted with MinerU (~19.8 GB) at
`--mem-fraction-static 0.65`:

- Weights: 18.5 GB, loaded in 3 s from local disk.
- Full KV pool: **598,144 tokens** (9.1 GB K + 9.1 GB V) plus a 59,776-token
  SWA pool (9.1 GB), 27.4 GB total. The 262K context therefore fits two
  full-length sequences, with 18.4 GB still available for graphs and
  multimodal workspace.
- Process total: ~60.2 GB; card total with MinerU: ~78.7 / 95.6 GB.
- Cold start: ~4.5 minutes Balanced, dominated by 161 s of prefill CUDA
  graph capture (58 shapes); ~113 s with DSpark once the kernel cache is
  warm.

With DSpark enabled the 1.27 GB draft head is loaded before the KV pool, so
the pool drops to **484,864 full tokens** (SWA 48,384) and 22.1 GB remains
available after graph capture. That still fits the 262K context twice over.

Do not assume 1M context fits on a 96 GB card just because the checkpoint
supports it: raise `--context-length` only after reading the startup KV
pool, and re-check the co-tenant's headroom first.

SGLang caps `max_running_requests` at 234 because of the Mamba state cache
(`max_mamba_cache_size=1172`, 5 slots per request). That is far above what
a single 96 GB card can fill with long contexts.

### Larger allocations

Scaling the measured anchor (484,864 tokens at 0.65) to a card with no
co-tenant and to TP2/EP2:

| Config | Mem fraction | KV budget | Pool tokens | 262K sessions | 1M sessions |
|---|---|---:|---:|---:|---:|
| 1 GPU | 0.65 (measured) | 22 GiB | 484,864 | 1.8 | 0.5 |
| 1 GPU | 0.80 | 37 GiB | ~800k | 3.0 | 0.8 |
| 1 GPU | 0.90 | 46 GiB | ~1.0M | 3.8 | 1.0 |
| 1 GPU | 1.00 (unsafe) | 56 GiB | ~1.2M | 4.6 | 1.2 |
| 2 GPUs TP2/EP2 | 0.80 | ~49 GiB/GPU | ~2.1M | 8.1 | 2.0 |
| 2 GPUs TP2/EP2 | 0.90 | ~58 GiB/GPU | ~2.5M | 9.7 | 2.4 |

These use ~48 KiB of KV per token at TP1 (~24 KiB per GPU at TP2) and
assume expert weights are EP-sharded, attention and KV are TP-sharded,
Mamba state halves, and graph memory stays fixed. The 2-GPU row is an
estimate (+/-10%): SGLang verifies TP1 on RTX PRO 6000, and EP must stay
equal to TP because the NVFP4 experts cannot be tensor-parallel sharded.

DSpark still caps `max_running_requests` at 4, so the larger pool on two
cards buys longer contexts, not more concurrent requests. At 0.9, one card
holds one 1M-token request and two cards hold two.

## Verification (measured 2026-11-21)

- `/v1/models` lists `inkling-2-mini` with `owned_by=sglang` and
  `max_model_len=262144`.
- Single-stream decode: **145.8 tok/s** Balanced and **150.6 tok/s** with
  DSpark on the same 512-reasoning-token prompt, on the co-tenanted card.
  DSpark bursts 150-222 tok/s with accept length 2.7 and accept rate 0.24
  (prompt-dependent; upstream reference is 168 tok/s on a quiet card).
- Text + image: image request used 7,676 image tokens and returned the
  correct animal in 3.9 s.
- Tool calling: `inkling2` parser returned a `get_weather` call with
  `{"city": "Lisbon"}` and `finish_reason=tool_calls`.
- Long context: 130,045-token needle-in-a-haystack prompt answered
  `ZEPHYR-7734` correctly. Cold prefill ~8.5k tok/s; a prefix-cache hit
  returned in 3.2 s.
- Gateway: `ycluster inference status` reports the model healthy and a
  chat completion through `local-ai-proxy` succeeds.

**Reasoning effort.** The chat template defaults to `reasoning_effort=0.9`
and accepts `[0.0, 0.99]`; pass it as `chat_template_kwargs` when a caller
needs shorter thinking. The model can spend a few hundred reasoning tokens
on long prompts, so short `max_tokens` values may return empty `content`
with `finish_reason=length` even though the reasoning is correct.

## Admin deployment

`admin/install-inkling-2-mini.yml` templates
`~/bin/inkling-2-mini-setup.sh` and the `inkling-2-mini` quadlet, and
registers the backend on the storage leader with `ycluster inference add`.
Per-host opt-in:

```yaml
inkling_enabled: true
inkling_gpus: "0"          # CDI index, not the nvidia-smi index
inkling_mem_fraction: 0.65
```

Defaults: port 9220, served name `inkling-2-mini`, context 262144,
container memory 64g, `inkling_dspark: false` (set true on this deployment;
restart the unit after changing it — the playbook does not bounce a running
engine).

Startup is gated on a staged checkpoint at
`/home/dev/models/Inkling-2-Mini/config.json`; run
`~/bin/inkling-2-mini-setup.sh --download` (uses
`HF_HUB_DISABLE_XET=1`) before the play can start the unit.
