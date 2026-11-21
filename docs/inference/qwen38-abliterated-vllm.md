# Qwen3.8-27B abliterated (NVFP4) on RTX PRO 6000 / SM120

Runtime: a vendored image built from
`config/ansible/admin/files/qwen38-vllm/Containerfile` — CUDA 13.0 devel
(`nvidia/cuda:13.0.0-devel-ubuntu24.04`, for `nvcc`) with vLLM 0.29.0 and
FlashInfer 0.6.18 from PyPI. The devel base is required because FlashInfer
JIT-compiles its attention and FP4 GEMM kernels at first launch and silently
disables itself when `nvcc` is missing.

## Checkpoint

- Repo: `sakamakismile/Huihui-Qwen3.8-27B-abliterated-NVFP4`
- An NVFP4 (W4A4, group 16) quant of `huihui-ai/Huihui-Qwen3.8-27B-abliterated`,
  itself a refusal-abliterated derivative of `Qwen/Qwen3.8-27B`.
- ~20 GB on disk (27.8B total params), ~19.6 GiB resident. The bundled MTP
  draft head stays in bf16 and is served with `qwen3_5_mtp`.
- Revision: currently `main`; pin the download SHA and the staged
  `config.json` hash after the first confirmed download. The image tag carries
  a build date, so a rebuild is explicit.

## Context and KV sizing

- Native context is 262,144 tokens. The deployment default is 65,536
  (`qwen38_max_model_len`), because the KV pool, not the architecture, is the
  limit at usable concurrency.
- The text model is hybrid: 64 layers, every 4th a full-attention layer
  (16 total) with 4 KV heads × 256 head dims; the other 48 are
  gated-delta-net (GDN) linear-attention layers.
- FP8 attention KV is `16 layers × 4 heads × 256 dims × 2 (K+V) × 1 B =
  32 KiB/token`. GDN state is fixed per sequence (~1.6 MiB per layer, ~78 MiB
  for 48 layers), not per token.
- vLLM aligns the attention block size to the GDN page size (~1600 tokens)
  and pads three GDN groups, so the effective pool is smaller than the raw
  byte math suggests. On a 96 GiB card at `--gpu-memory-utilization 0.45`
  (~43 GiB budget) the pool lands near 19 GiB, about 6.5 concurrent
  full-64K requests. Raise the window only with that ratio in mind: 128K
  halves it, 256K is effectively single-stream.

## Launch

```
vllm serve /model \
  --served-model-name qwen3.8-27b-abliterated \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --max-model-len 65536 \
  --max-num-seqs 64 \
  --gpu-memory-utilization 0.45 \
  --kv-cache-dtype fp8 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --speculative-config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3}' \
  --host 0.0.0.0 --port 9221
```

Per-host knobs live in `host_vars` (`qwen38_gpus`, `qwen38_port`,
`qwen38_served_name`, `qwen38_gpu_mem_util`, `qwen38_max_model_len`); the
playbook only installs the scripts and registers the gateway route.

## Non-obvious flags and failure modes

- **`MAX_JOBS` on first launch.** FlashInfer compiles ~17 nvcc targets. With
  the default parallel fan-out this exhausts a container memory cap long
  before the engine starts (nvcc children are OOM-killed, ninja exits 137).
  The start script defaults `MAX_JOBS=2`; the compiled kernels persist in the
  `CACHE_DIR` volume, so this cost is one-time per cache.
- **`--max-num-seqs` must fit the GDN block count.** vLLM's 1024 default
  exceeds the Mamba cache blocks and aborts during CUDA graph capture with
  `max_num_seqs exceeds available Mamba cache blocks`. 64 is well inside the
  limit and above the attention-limited concurrency at 64K.
- **Tool choice.** The chat template emits Qwen3's
  `<tool_call><function=...><parameter=...>` XML, so `qwen3_coder` is the
  matching parser. Without both tool flags, any request carrying
  `tool_choice: "auto"` is rejected by vLLM. This is an abliterated
  fine-tune: the parser accepts the format, but tool-call reliability is not
  validated the way the base model's is.
- **Co-tenancy.** The start script supports a shared card through
  `qwen38_gpu_mem_util`. The concrete split against another backend belongs
  in `ycluster-private` host vars, not here.

## Verification

- `~/bin/qwen38-vllm-setup.sh` builds the image, CDI-smokes the card, and
  verifies the staged checkpoint.
- `~/bin/qwen38-vllm-start.sh` is idempotent and replaces the container.
- `curl -fsS http://127.0.0.1:<port>/health` returns 200 only after the JIT
  and CUDA graph capture finish (minutes on a cold cache).
- A real check is a chat completion through the gateway with
  `chat_template_kwargs.enable_thinking = false` (the reasoning preamble
  otherwise eats a small token budget), plus one request with a tool
  definition and `tool_choice: "auto"`; the latter should return
  `finish_reason: tool_calls`.
