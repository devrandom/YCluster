# MiniMax-M3 4-bit on RTX PRO 6000 (SM120)

Serving MiniMax-M3 (4-bit quant, 428B total / 23B active multimodal MoE,
MiniMax Sparse Attention, up to 1M context) on a node with 4× NVIDIA RTX PRO
6000 Blackwell (compute capability 12.0, "SM120").

## TL;DR

**Current prod (2026-07-02): stock vLLM 0.24 + MXFP4 + EAGLE3.** `docker.io/vllm/vllm-openai:v0.24.0`
with the `olka-fi/MiniMax-M3-MXFP4` checkpoint, one 3-line clamp patch
(auto-derived by `~dev/bin/minimax-m3-stock-setup.sh`), `--block-size 128`,
and EAGLE3 speculative decoding ON: coherent on SM120 at **117–169 tok/s
single-stream**. Managed by `install-minimax-m3.yml`; start with
`~dev/bin/minimax-m3-stock-start.sh`.

The previous b12x NVFP4 stack is archived at
[`docs/archive/minimax-m3-b12x-stack.md`](../../archive/minimax-m3-b12x-stack.md).
We are not going back to it — stock covers everything we need, with full
upstream parsers and 1.5–2.2× the b12x baseline. Stock *NVFP4* paths remain
broken on SM120 (the b12x doc has the kernel investigation); MXFP4 via Marlin
weight-only is the format that works on stock.

## Why NVFP4 is broken on SM120 (kept as background)

The CUTLASS NVFP4 grouped-GEMM tactics are `sm_100a`-only; on SM120 every
autotuner tactic silently fails. SGLang funnels NVFP4 experts through
`cutlass_moe_fp4` regardless of `--moe-runner-backend`, so no backend flag
escapes it:

| MoE backend (stock SGLang image) | Result on SM120 |
|----------------------------------|-----------------|
| `flashinfer_cutlass`             | NaN logits → null-byte output |
| `flashinfer_cutlass` + cuDNN dense GEMM | still NaN (NaN is in the MoE, not dense GEMM) |
| `flashinfer_trtllm`              | startup crash (`trtllm_bf16_moe` arg mismatch) |
| `triton`                         | device-side assert in `nvfp4_blockwise_moe.cuh` |
| `flashinfer_cutedsl`             | illegal memory access |

The same NaN/garbage symptom is reported across other NVFP4 models on this
hardware (Qwen3.5, GLM-5, MiMo-V2.5) — it is the kernel, not the checkpoint.
Upstream tracking: SGLang PR #20047 (default FP4 GEMM → flashinfer_cudnn on
SM120, dense only), issue #21314 (CUTLASS SM120 grouped-GEMM). This is why
the current stack uses **MXFP4** (Marlin weight-only), which works on stock.

**Corollary — verify output, not liveness.** A bare `/v1/models` 200 and a
short temp-0 completion both pass on the broken stack (a reasoning model with
`max_tokens: 8` returns empty `content` even when healthy). Always read actual
generated tokens.

## Prerequisites

- 4 free RTX PRO 6000 GPUs, NVIDIA open driver + container toolkit
  (`install-nvidia.yml`).
- Rootless podman as `dev` with a working nvidia CDI spec — provided by
  `admin/setup-gpu-podman.yml` plus the custom podman 5.8.0 build from
  `admin/install-podman.yml` (the distro podman 4.9.3 can't parse the CDI
  0.7.0 spec that recent nvidia-container-toolkit generates). GPU group
  access is preserved at run time by `--group-add keep-groups`.
- ~240 GB free disk for the MXFP4 checkpoint.

## 1. Fetch and verify the checkpoint

```bash
# as dev
hf download olka-fi/MiniMax-M3-MXFP4 \
  --local-dir /home/dev/models/MiniMax-M3-MXFP4-olka
```

Verify integrity against HF's published hashes before trusting it — the API
exposes each file's sha256 as `lfs.oid`:

```bash
curl -fsS -H "Authorization: Bearer $(cat ~/.cache/huggingface/token)" \
  'https://huggingface.co/api/models/olka-fi/MiniMax-M3-MXFP4/tree/main?recursive=1' \
  | jq -r '.[] | select(.path|endswith(".safetensors")) | "\(.lfs.oid)  \(.path)"' \
  > /tmp/expected.sha256
# then compare with `sha256sum` of each local shard (expect all match)
```

## 2. Pull the image

```bash
podman pull docker.io/vllm/vllm-openai:v0.24.0
```

## Scripts (deployed via ansible)

Host prerequisites (podman + nvidia CDI) are generic ansible, run once per
machine by an operator:

```bash
./run-playbook.sh admin/setup-gpu-podman.yml --limit <node>
```

`admin/install-minimax-m3.yml` then deploys dev scripts into `~dev/bin`
(the image tag is a var in that playbook — bump it there to ship a new
release):

```bash
./run-playbook.sh admin/install-minimax-m3.yml --limit <node>
```

The dev user needs no sudo for these:

| Script | When |
|--------|------|
| `~dev/bin/minimax-m3-stock-setup.sh` | first run **and after a new image release** — `podman pull` + derive the clamp patch |
| `~dev/bin/minimax-m3-stock-start.sh`  | (re)launch the server, e.g. after a reboot |

`install-minimax-m3.yml` also still deploys the b12x scripts (`minimax-m3-b12x-setup.sh`,
`minimax-m3-b12x-start.sh`) into `~dev/bin` for archaeology; they are inert in current prod.
See [`docs/archive/minimax-m3-b12x-stack.md`](../../archive/minimax-m3-b12x-stack.md).

## 3. Launch

`minimax-m3-stock-start.sh` runs the following (rootless podman, GPUs 0–3,
leaving the rest free):

```bash
podman run -d --name minimax-m3-stock \
  --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 \
  --device nvidia.com/gpu=2 --device nvidia.com/gpu=3 \
  --network=host --shm-size 32g \
  --security-opt seccomp=unconfined --group-add keep-groups \
  --ulimit stack=67108864:67108864 \
  --memory 26g --memory-swap -1 \
  -e CUDA_VISIBLE_DEVICES=0,1,2,3 \
  -e VLLM_MARLIN_USE_ATOMIC_ADD=1 \
  -v /home/dev/models/MiniMax-M3-MXFP4-olka:/models/MiniMax-M3-MXFP4:ro \
  -v /home/dev/models/MiniMax-M3-EAGLE3:/models/MiniMax-M3-EAGLE3:ro \
  -v /home/dev/vllm-patches/compressed_tensors_moe_w4a4_mxfp4.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/quantization/compressed_tensors/compressed_tensors_moe/compressed_tensors_moe_w4a4_mxfp4.py:ro \
  -v /opt/inference/minimax-m3/chat-template.jinja:/opt/inference/minimax-m3/chat-template.jinja:ro \
  --entrypoint vllm \
  docker.io/vllm/vllm-openai:v0.24.0 \
  serve /models/MiniMax-M3-MXFP4 \
  --served-model-name brandonmusic/minimax-m3-nvfp4 MiniMax-M3-NVFP4 minimax-m3 \
  --tensor-parallel-size 4 --trust-remote-code \
  --kv-cache-dtype bfloat16 --block-size 128 \
  --max-model-len 256000 --max-num-seqs 64 \
  --max-num-batched-tokens 1024 \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image":0,"video":0}' \
  --default-chat-template-kwargs '{"thinking_mode":"enabled"}' \
  --chat-template /opt/inference/minimax-m3/chat-template.jinja \
  --reasoning-parser minimax_m3 \
  --enable-auto-tool-choice --tool-call-parser minimax_m3 \
  --linear-backend marlin \
  --speculative-config '{"method":"eagle3","model":"/models/MiniMax-M3-EAGLE3","num_speculative_tokens":3}' \
  --host 0.0.0.0 --port 9211
```

Load is ~5 min warm (no torch.compile pass, unlike the retired b12x stack's
~15–20 min cold). See `~dev/bin/minimax-m3-stock-start.sh` for the full
parameterized version with env overrides (`TP`, `GPUS`, `MAXLEN`, `MAXSEQS`,
`MAXBATCH`, `GPU_UTIL`, `PORT`, `M3_EAGLE3`).

### Key flag rationale

Each non-obvious flag below earned its place via a distinct failure mode at
256k context.

- **`--block-size 128`** (MSA page size, mandatory): M3's MSA backend declares
  `get_supported_kernel_block_sizes() == [128]` (one sparse block per KV page);
  the default (16) and 64 both die in KV-cache init with `ValueError: No common
  block size`. The selector only rounds down, so 128 is the single valid value.
- **`--kv-cache-dtype bfloat16`**: MSA sparse backend is bf16-KV only and
  hard-fails on fp8_e4m3. Accepted value is `bfloat16` (not `bf16` — argparse
  rejects `bf16`).
- **`--linear-backend marlin`**: ~+2 tok/s single-stream on Blackwell;
  FlashInfer NVFP4 kernels hard-gate to SM100.
- **`--max-num-seqs 64`**: vLLM v1 preallocates pinned CPU buffers scaling with
  `max_num_seqs × max_model_len` per worker. At 0.24's default seqs and 256k
  that's ~6 GiB anon per worker, and pinned pages can't swap — the kernel
  OOM-kills a worker silently (only symptom: `Worker proc VllmWorker-N died
  unexpectedly`; check `journalctl -k` for `oom-kill`). 64 concurrent seqs
  is ample for us (the retired b12x recipe ran with 4).
- **`--max-num-batched-tokens 1024`** (`MAXBATCH`): vLLM v1's chunked-prefill
  scheduler co-schedules decodes with prefill chunks — every running
  generation gets exactly one iteration per engine step, so decode speed
  during a prefill is 1/step-time (× EAGLE3 acceptance). At the v1 default of
  8192, one Marlin prefill chunk takes ~0.5–1 s, so every concurrent
  generation drops to ~1–3 tok/s for the duration. Capping the chunk at 1024
  measured out: generation holds ~5–15 tok/s through the prefill window,
  recovering fully when prefill ends. Don't go smaller: cold prefill ran ~2k
  tok/s at 1024, and 512 collides with spec-decode's reserved draft slots.
- **`--limit-mm-per-prompt '{"image":0,"video":0}'`**: the checkpoint is the
  VL variant, and the multimodal *encoder cache* scales with context — ~15
  GiB GPU at 256k, leaving only 2.7 GiB for KV. We serve text-only; zeroing
  image/video skips the encoder entirely.
- **`--default-chat-template-kwargs '{"thinking_mode":"enabled"}'`**: M3's
  default "adaptive" system prompt *literally contains*
  `<mm:think></mm:think>` as instructional text, tokenized as the real marker
  tokens. The reasoning parser's prompt back-scan hits that closer, decides
  reasoning already ended, and bypasses parsing for the whole stream (OWUI
  then renders tag spam; non-streaming is unaffected, which is why simple
  probes pass). `thinking_mode=enabled` prefills `<mm:think>` at the prompt end
  so the scan lands on a start marker and streaming parses correctly. Side
  effect: thinking is always on, not adaptive (per-request
  `chat_template_kwargs` still override). **Upstream vLLM bug** — `main`'s
  `is_reasoning_end` has the same last-marker semantics; worth filing.
- **`--reasoning-parser minimax_m3 --enable-auto-tool-choice --tool-call-parser
  minimax_m3`**: vLLM 0.24 carries upstream's `minimax_m3` reasoning + tool
  parsers built in (PR #45381, merged 2026-06-15). The reasoning parser
  sends `<mm:think>` to the `reasoning` field; the tool parser emits structured
  `tool_calls` for `tool_choice: auto` (verified streaming and non-streaming
  — Open-WebUI native tool calling works). Without these flags OWUI errors
  with "auto tool choice requires --enable-auto-tool-choice and
  --tool-call-parser to be set". Stock vLLM 0.24 fully parses M3's XML
  format; tool-using clients (opencode, etc.) no longer need to be routed
  to a different model.
- **`--speculative-config '{"method":"eagle3",...}'`** (EAGLE3 ON by default):
  Stock's MSA backend keeps **FULL decode cudagraphs under spec-decode** —
  exactly what the retired b12x `B12X_ATTN` couldn't — so the 80% draft
  acceptance finally converts into wall-clock speed. Do NOT add
  `attention_backend` to the spec-config: vLLM picks the right drafter
  backend itself.
- **`VLLM_MARLIN_USE_ATOMIC_ADD=1`**: no measured change; harmless.

### Tool-call parser failure mode — malformed XML

The Rust `minimax_m3` tool parser rejects any tool-call block whose tags
don't match the patched chat template's envelope exactly. Symptoms: vLLM
logs `ValueError: tool parser parsing failed` from `MinimaxM3ToolParser`; the
response comes back with `finish_reason: "tool_calls"` but an empty
`tool_calls` array — the client sees a successful tool-call turn with no
actual call to execute. Common bad shapes from M3: open/close tag mismatch
(e.g. `<command>…</cmd>` instead of `<invoke name="…">…</invoke>`), missing
closing `</tool_call>`, wrong namespace token, or `<param>` blocks outside an
`<invoke>`. The `contrib/inference chat` smoke test catches this — its
`tool-call stream` check flags empty `name` / empty `args` as FAIL even when
`finish_reason` is `tool_calls`. Mitigation in flight: constrained generation
via vLLM `--guided-decoding-backend xgrammar --guided-grammar <lark>` to
constrain M3 to emit the template's exact envelope, mirroring the llama.cpp
`autoparser` derivation in `ext/llama.cpp/docs/autoparser.md` (which already
classifies MiniMax-M2 as `TAG_WITH_JSON` and could lift directly to M3's
namespace-prefixed `TAG_WITH_TAGGED` variant).

### The clamp patch (auto-derived)

A one-line pre-ForwardHook patched file bind-mounted over the image's
`compressed_tensors_moe_w4a4_mxfp4.py`: upstream drops the model's
clamped-SwiGLU params, so `MarlinExperts` asserts `SWIGLUOAI_UNINTERLEAVE
requires clamp_limit` at first forward. The fix threads
`layer.swiglu_{alpha,beta,limit}` into `make_mxfp4_moe_quant_config` (all
downstream plumbing exists). `minimax-m3-stock-setup.sh` derives the
patched file from the image with an anchored replace that fails loudly if
upstream changes. **Candidate for an upstream vLLM PR.**

## 4. Verify it actually works

```bash
curl -fsS http://127.0.0.1:9211/v1/models | jq -r '.data[].id'   # -> brandonmusic/minimax-m3-nvfp4, MiniMax-M3-NVFP4, minimax-m3

curl -fsS -H 'Content-Type: application/json' -d '{
  "model":"brandonmusic/minimax-m3-nvfp4",
  "messages":[{"role":"user","content":"What is 17 times 24? Give just the number."}],
  "max_tokens":200,"temperature":0}' \
  http://127.0.0.1:9211/v1/chat/completions | jq '.choices[0].message.content'
```

Expect coherent text with `finish_reason: "stop"` (reasoning is inline in
`<mm:think>…</mm:think>` tags), e.g. the 17×24 answer is `408`. Empty content,
null bytes (`\0`), or `finish_reason: "length"` on a temp-0 prompt mean the
kernel path is wrong — see "Why NVFP4 is broken on SM120" above.

**Deeper verification (`contrib/inference-smoke-test.py`).** A re-runnable,
model-agnostic harness (hits the gateway via `contrib/_cluster_config.py`; run
after any image bump) covers three suites:

- **Quality** — fixed temp=0 prompts including quant-degradation traps
  (bat-and-ball, letter counting, sibling logic). A clean quant passes all and
  leaks no `<mm:think>` tags; failures here flag silent kernel/quant drift.
- **Long context (MSA)** — needle-in-haystack; validated clean retrieval at
  positions 10/50/90% up to **~168k prompt tokens** (`finish_reason: stop`, no
  NaN), exercising the sparse-attention path. **Disable thinking** for this
  (`chat_template_kwargs: {"thinking_mode":"disabled"}`) or give a large
  `max_tokens` — otherwise reasoning eats the budget and the answer truncates to
  empty content / `finish_reason: length`, which looks like a retrieval failure
  but isn't.
- **Vision** — M3 is **multimodal and the path is enabled** on this image: it
  reads both normal and wide images (no tiling failure), via the standard OpenAI
  `image_url` content blocks.

**Throughput / latency.** Serving benchmark (concurrency sweep, `vllm bench
serve`): [../benchmarks/minimax-m3-nvfp4-vllm-serve.md](../benchmarks/minimax-m3-nvfp4-vllm-serve.md).

**Verify after any restart or image bump** with `./contrib/inference chat
minimax-m3` (from a machine with gateway config): it streams a completion and
then explicitly checks for think-tag leaks in streamed content and for working
streamed `tool_choice: auto` tool calls, exiting non-zero on failure.

## 5. Register with the inference gateway

Once verified, add the backend so `local-ai-proxy` routes to it
(see [../operations/inference.md](../operations/inference.md)):

```bash
ycluster inference add http://nvN.xc:9211
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `unresolvable CDI devices nvidia.com/gpu=N` | stale CDI spec (device set changed) or the distro podman 4.9.3 still installed | regenerate `/etc/cdi/nvidia.yaml`; ensure custom podman 5.8.0 is installed (`docs/operations/cdi-spec-regen.md`) |
| `Config not found for minimax-m3-vl` then engine init fails | wrong checkpoint | use `olka-fi/MiniMax-M3-MXFP4` (these `[ERROR]` lines are non-fatal warnings on the correct checkpoint) |
| `MSA sparse backend v1 is bf16-KV only (got fp8_e4m3)` | KV cache resolved to fp8 | append `--kv-cache-dtype bfloat16` |
| `invalid choice: 'bf16'` | wrong spelling | use `bfloat16` |
| `ValueError: No common block size for 16` (or 64) in KV-cache init | MSA requires KV page size 128 exactly | pass `--block-size 128` |
| One token repeated to the cap, temp 0, any prompt | wrong quant format | use the **MXFP4** checkpoint (`olka-fi/MiniMax-M3-MXFP4`); NVFP4 on stock vLLM is broken on SM120 — see "Why NVFP4 is broken on SM120" |
| `AssertionError: SWIGLUOAI_UNINTERLEAVE requires clamp_limit` at first forward | compressed-tensors MoE path drops the clamped-SwiGLU params | run `~dev/bin/minimax-m3-stock-setup.sh` (derives the clamp patch); start script mounts it |
| `Worker proc VllmWorker-N died unexpectedly` during startup, no traceback | host-RAM OOM (kernel kill) — pinned CPU buffers scale with `max_num_seqs × max_model_len` | use the start script (`--max-num-seqs 64` + cgroup cap + unlimited swap); check `journalctl -k` for `oom-kill` |
| `To serve at least one request with the model's max seq len ... available KV cache memory (2.7 GiB)` at 256k | vision encoder cache scales with context (~15 GiB GPU) on the VL checkpoint | `--limit-mm-per-prompt '{"image":0,"video":0}'` (text-only serving) |
| Streamed `content` contains raw `<mm:think>` tags but non-streaming is clean | M3's adaptive system prompt embeds literal think markers, defeating the reasoning parser's prompt scan | `--default-chat-template-kwargs '{"thinking_mode":"enabled"}'`; verify with `./contrib/inference chat` |
| `MinimaxM3ToolParser` logs `ValueError: tool parser parsing failed`; response has `finish_reason: "tool_calls"` but empty `tool_calls` | M3 emitted malformed XML (open/close tag mismatch, e.g. `<command>…</cmd>` vs `<invoke name="…">…</invoke>`, missing `</tool_call>`, wrong namespace token) | Retry the prompt — usually succeeds on resample. Long-term: constrain generation with `--guided-decoding-backend xgrammar --guided-grammar <lark>` matching the patched chat template's XML envelope |

## Remaining watch-items

- Upstream the clamp fix (removes the patch).
- FlashInfer MXFP4/NVFP4 kernels gaining SM120 (native FP4 tensor cores
  would lift prefill/batch throughput above Marlin weight-only, which is
  decode-friendly but compute-poor).
