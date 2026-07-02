# MiniMax-M3 4-bit on RTX PRO 6000 (SM120)

Serving MiniMax-M3 (4-bit quant, 428B total / 23B active multimodal MoE,
MiniMax Sparse Attention, up to 1M context) on a node with 4× NVIDIA RTX PRO
6000 Blackwell (compute capability 12.0, "SM120").

## TL;DR

**Current prod (2026-07-02): stock vLLM 0.24 + MXFP4 + EAGLE3** — see
"Stock vLLM MXFP4 + EAGLE3" below. `docker.io/vllm/vllm-openai:v0.24.0` with
the `olka-fi/MiniMax-M3-MXFP4` checkpoint, one 3-line clamp patch
(auto-derived by `~dev/bin/minimax-m3-stock-setup.sh`), `--block-size 128`,
and EAGLE3 speculative decoding ON: coherent on SM120 at **117–169 tok/s
single-stream** (1.5–2.2× the b12x baseline). Managed by
`install-minimax-m3.yml`; start with `~dev/bin/minimax-m3-stock-start.sh`.

The previous prod stack — and the fallback if stock misbehaves — is
lukealonso's **b12x** kernel family (CuTe DSL NVFP4 grouped-GEMM for SM120),
packaged in the `verdictai/minimax-m3-nvfp4-b12x` image, paired with the
`brandonmusic/MiniMax-M3-NVFP4` checkpoint, with the KV cache forced to
`bfloat16` (`~dev/bin/minimax-m3-b12x-start.sh`). The stock SGLang/cutlass
*NVFP4* MoE path remains **broken on SM120** — it loads and serves but
produces NaN/garbage tokens (details below); MXFP4 via Marlin weight-only is
the format that works on stock. Both stacks run as rootless-podman containers
under the `dev` user, TP=4, OpenAI-compatible on port 9211.

## Why the obvious path fails

The CUTLASS NVFP4 grouped-GEMM tactics are `sm_100a`-only; on SM120 every
autotuner tactic silently fails. SGLang funnels NVFP4 experts through
`cutlass_moe_fp4` **regardless of `--moe-runner-backend`**, so no backend flag
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
The fix is a different kernel family (b12x), not a flag. Upstream tracking:
SGLang PR #20047 (default FP4 GEMM → flashinfer_cudnn on SM120, dense only),
issue #21314 (CUTLASS SM120 grouped-GEMM).

**Corollary — verify output, not liveness.** A bare `/v1/models` 200 and a
short temp-0 completion both pass on the broken stack (a reasoning model with
`max_tokens: 8` returns empty `content` even when healthy). Always read actual
generated tokens.

## Working stack

- **Image**: `docker.io/verdictai/minimax-m3-nvfp4-b12x:v1` — vLLM + b12x
  0.11.2.dev (swiglu-correct MoE + native MSA sparse attention). Third-party
  community image; published on the brandonmusic checkpoint card.
- **Checkpoint**: `brandonmusic/MiniMax-M3-NVFP4` (~250 GB). Do **not** use
  `bullerwins/MiniMax-M3-NVFP4` — it is repacked for the SGLang/0xSero loader
  and its `minimax_m3_vl` config is rejected by this image's vLLM loader.
- **Kernel selection**: handled inside the image (`VLLM_USE_B12X_MOE=1`); the
  entrypoint runs `serve_m3.py`.

## Prerequisites

- 4 free RTX PRO 6000 GPUs, NVIDIA open driver + container toolkit
  (`install-nvidia.yml`).
- Rootless podman as `dev` with a working nvidia CDI spec — provided by
  `admin/setup-gpu-podman.yml`. (It works around a podman/CDI incompatibility:
  podman 4.9.3 can't parse nvidia-ctk 1.19's CDI 0.7.0 spec / `additionalGids`,
  so the playbook pins the spec to 0.6.0 and strips that field; otherwise every
  `--device nvidia.com/gpu=N` fails with "unresolvable CDI devices".) GPU group
  access is preserved at run time by `--group-add keep-groups`.
- ~260 GB free disk for the checkpoint.

## 1. Fetch and verify the checkpoint

```bash
# as dev
hf download brandonmusic/MiniMax-M3-NVFP4 \
  --local-dir /home/dev/models/MiniMax-M3-NVFP4-brandonmusic
```

Verify integrity against HF's published hashes before trusting it — the API
exposes each file's sha256 as `lfs.oid`:

```bash
curl -fsS -H "Authorization: Bearer $(cat ~/.cache/huggingface/token)" \
  'https://huggingface.co/api/models/brandonmusic/MiniMax-M3-NVFP4/tree/main?recursive=1' \
  | jq -r '.[] | select(.path|endswith(".safetensors")) | "\(.lfs.oid)  \(.path)"' \
  > /tmp/expected.sha256
# then compare with `sha256sum` of each local shard (expect all match)
```

## 2. Pull the image

```bash
podman pull docker.io/verdictai/minimax-m3-nvfp4-b12x:v1
```

## Scripts (deployed via ansible)

Host prerequisites (podman + nvidia CDI) are generic ansible, run once per
machine by an operator:

```bash
./run-playbook.sh admin/setup-gpu-podman.yml --limit <node>
```

`admin/install-minimax-m3.yml` then deploys two dev scripts into `~dev/bin`
(the image tag is a var in that playbook — bump it there to ship a new
release):

```bash
./run-playbook.sh admin/install-minimax-m3.yml --limit <node>
```

The dev user needs no sudo for these:

| Script | When |
|--------|------|
| `~dev/bin/minimax-m3-b12x-setup.sh` | first run **and after a new image release** — `podman pull` + GPU check |
| `~dev/bin/minimax-m3-b12x-start.sh` | (re)launch the server, e.g. after a reboot |

A new image release is a pure-dev path: `setup.sh` (re-pull) then `start.sh`.
The manual steps below are what these scripts encapsulate.

## 3. Launch

`minimax-m3-b12x-start.sh` runs the following (rootless podman, GPUs 0–3,
leaving the rest free):

```bash
#!/usr/bin/env bash
set -euo pipefail
IMAGE=docker.io/verdictai/minimax-m3-nvfp4-b12x:v1
MODEL_HOST=/home/dev/models/MiniMax-M3-NVFP4-brandonmusic
CONTAINER=minimax-m3-b12x
PORT=9211

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

podman run -d --name "$CONTAINER" \
  --device nvidia.com/gpu=0 --device nvidia.com/gpu=1 \
  --device nvidia.com/gpu=2 --device nvidia.com/gpu=3 \
  --network=host --shm-size 32g \
  --security-opt seccomp=unconfined --group-add keep-groups \
  --ulimit stack=67108864:67108864 \
  -e TP=4 -e GPU_UTIL=0.93 -e MAXLEN=262144 -e MAXSEQS=16 -e PORT="$PORT" \
  -e VLLM_CACHE_ROOT=/cache \
  -v "$MODEL_HOST":/model \
  -v /home/dev/.cache/minimax-m3-vllm:/cache \
  "$IMAGE" \
    --kv-cache-dtype bfloat16 \
    --served-model-name brandonmusic/minimax-m3-nvfp4
```

First start runs a JIT pre-warm (expect `warm_jit: fused M3 swigluoai
compiled`) then loads ~250 GB across the 4 GPUs. Served model id:
`brandonmusic/minimax-m3-nvfp4` — deliberately prefixed with the quant author
so it's never mistaken for MiniMax's official M3 (the image's default is
`minimax-m3-nvfp4`; the trailing `--served-model-name` overrides it). Env
knobs: `TP`, `GPU_UTIL`, `MAXLEN` (context), `MAXSEQS` (concurrency), `PORT`.

**Load time** (~15–20 min): weight load ~2.5 min, `torch.compile` ~40 s, and
the bulk is **CUDA-graph capture ~7 min** (mostly the FULL decode graphs). The
persistent `/cache` mount (`VLLM_CACHE_ROOT`, plus the image's own
`XDG_CACHE_HOME=/cache/jit`) reuses the compile/JIT artifacts across
`podman rm` — but **graph capture captures live memory pointers, so it isn't
cacheable and re-runs every start**. So a warm reboot only trims the compile
time; cutting `cudagraph_mode`/sizes is the lever for the capture cost.

### The one flag that matters: `--kv-cache-dtype bfloat16`

The entrypoint defaults to `--kv-cache-dtype auto`, which resolves to
`fp8_e4m3` for an FP4 model. The MSA sparse backend is **bf16-KV only** and
hard-fails:

```
NotImplementedError: MSA sparse backend v1 is bf16-KV only (got fp8_e4m3)
```

The trailing arg overrides it (argparse takes the last `--kv-cache-dtype`).
The accepted value is `bfloat16` — **not** `bf16` (argparse rejects `bf16`).

### Reasoning parser (so Open-WebUI folds the thinking)

M3 emits its chain-of-thought inline as `<mm:think>…</mm:think>`. Without a
reasoning parser, vLLM leaves it in `content` and Open-WebUI shows it raw.
Upstream vLLM has a built-in `minimax_m3` parser, but **this image's vLLM build
predates it**, so we ship the upstream parser file
(`config/ansible/admin/files/minimax_m3_reasoning_parser.py`, deployed to
`/opt/inference/minimax-m3/`) with a one-line `register_module` decorator added,
and load it at launch:

```
--reasoning-parser-plugin /opt/inference/minimax-m3/minimax_m3_reasoning_parser.py \
--reasoning-parser minimax_m3
--default-chat-template-kwargs '{"thinking_mode":"enabled"}'
```

`api_server.py` calls `import_reasoning_parser(...)` on the plugin path at
startup, which registers the parser; vLLM then extracts the thinking into the
`message.reasoning` field (this build's name; older vLLM used
`reasoning_content`) and leaves `content` clean — verified in both streaming
and non-streaming. Drop these flags (and this whole plugin) once you rebuild on
a vLLM carrying upstream's native M3 support — PR #45381 (2026-06-15) ships the
built-in `minimax_m3` reasoning parser. Our image's fork point (2026-06-08)
predates it; see
[b12x-reproducible-build-design-2026-06.md](b12x-reproducible-build-design-2026-06.md).

**Why `--default-chat-template-kwargs thinking_mode=enabled` (and the
caveat).** The parser only streams correctly when the prompt *prefills*
`<mm:think>` (so generation begins inside the reasoning block). With thinking
left adaptive/undefined, M3 emits `<mm:think>` as its first generated token —
but vLLM's one-time prompt-check `is_reasoning_end(prompt)` scans backward and
hits the `</mm:think>` that appears *literally in the chat template's
thinking-instructions text*, latching "reasoning ended" before generation and
disabling streaming extraction (raw tags leak to every frontend). Forcing
`thinking_mode=enabled` guarantees the prefill — at the cost of M3 reasoning on
**every** turn (frontends can still override per request). The proper fix is to
override `is_reasoning_end` in our plugin to ignore the empty
`<mm:think></mm:think>` instruction pair and restore adaptive thinking — see
TODO.md.

### Tool calling — unsupported on this image

This deployment sets **no** `--tool-call-parser`, so vLLM cleanly refuses
`tool_choice: auto` (`"... requires --enable-auto-tool-choice and
--tool-call-parser to be set"`). That's intentional: M3 emits a custom **XML**
tool format — `<tool_call><invoke name="fn"><param>val</param>…</invoke></tool_call>`
(with a namespace token prefix) — which **none** of this image's vLLM tool
parsers match (`minimax`/`minimax_m2` expect `<tool_calls>` *plural* with JSON).
Upstream's `minimax_m3` tool parser handles it but is **Rust-based**
(`rust/src/tool-parser/src/minimax_m3.rs`, added in PR #45381 on 2026-06-15) and
absent from this build, so — unlike the reasoning parser — it can't be
backported as a python plugin. Enabling a mismatched parser would only leak raw
`<tool_call>` XML, so we leave tools off and let vLLM refuse them. **Route
tool-using clients (opencode, etc.) to a tool-capable model** (e.g.
`nvidia/MiniMax-M2.7-NVFP4` or the Nemotron models). The real fix is rebuilding
on a vLLM that carries #45381 (our fork point predates it) — see
[b12x-reproducible-build-design-2026-06.md](b12x-reproducible-build-design-2026-06.md).
Tracked in TODO.md.

## 4. Verify it actually works

```bash
curl -fsS http://127.0.0.1:9211/v1/models | jq -r '.data[].id'   # -> brandonmusic/minimax-m3-nvfp4

curl -fsS -H 'Content-Type: application/json' -d '{
  "model":"brandonmusic/minimax-m3-nvfp4",
  "messages":[{"role":"user","content":"What is 17 times 24? Give just the number."}],
  "max_tokens":200,"temperature":0}' \
  http://127.0.0.1:9211/v1/chat/completions | jq '.choices[0].message.content'
```

Expect coherent text with `finish_reason: "stop"` (reasoning is inline in
`<mm:think>…</mm:think>` tags), e.g. the 17×24 answer is `408`. Empty content,
null bytes (`\0`), or `finish_reason: "length"` on a temp-0 prompt mean the
kernel path is wrong — see "Why the obvious path fails".

**Deeper verification (`contrib/inference-smoke-test.py`).** A re-runnable,
model-agnostic harness (hits the gateway via `contrib/_cluster_config.py`; run
after any image bump) covers three suites, all green on the b12x image:

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
serve`): [../benchmarks/minimax-m3-nvfp4-vllm-serve.md](../benchmarks/minimax-m3-nvfp4-vllm-serve.md)
— ~75 tok/s single-stream, ~437 output tok/s saturated at conc 16.

## Speculative decoding — MTP unavailable, EAGLE3 the viable path

Single-stream decode (~75–89 tok/s) is near the no-NVLink TP=4 memory-bandwidth
floor for a 23B-active model; the only lever with step-change (~1.5–2.5×) upside
is speculative decoding. Investigated 2026-07-01.

**MTP is ruled out — the head does not exist in any M3 checkpoint.** M3's config
declares `num_nextn_predict_layers: 1`, but the *weights were never released*.
Verified directly off the on-disk checkpoint (`model.safetensors.index.json`) and
against HF: all 12 published M3 NVFP4 quants — including `nvidia/MiniMax-M3-NVFP4`
and `lukealonso/MiniMax-M3-NVFP4` (the b12x kernel author's own) — carry exactly
layers 0–59 and **zero** `nextn`/`eh_proj`/`enorm`/`hnorm`/`shared_head` tensors,
as does the **official base `MiniMaxAI/MiniMax-M3`** itself. So `--speculative-config
mtp,N` has nothing to load, and it is *not* recoverable by swapping quants (none
ship it) nor by quantizing it ourselves (the base has no MTP weights to quantize).
This is a stronger "no" than GLM-5.2, where the MTP layer at least exists as bf16
(see [glm-5.2-nvfp4-sm120-design-2026-06.md](glm-5.2-nvfp4-sm120-design-2026-06.md)).

**EAGLE3 is the live path — it needs no MTP layer.** `Inferact/MiniMax-M3-EAGLE3`
is a separately-trained draft: a **1-layer dense Llama (~3.3 B, bf16**, `hidden_size
6144`, shares embeddings + LM head with the target), trained via TorchSpec, with an
**official vLLM+NVIDIA day-0 M3 integration**. Because the draft is a single *dense*
layer — not a 256-expert MoE — its bf16 per-token cost is negligible against M3's
60-layer forward, so the GLM bf16-draft trap (a bf16 MoE draft costing ≈ a full main
forward) **does not apply here**. Correctness holds regardless of target quant (the
target verifies every token). Published acceptance (TP=4, `num_speculative_tokens 3`):
GSM8K mean-accept-len 3.52 (83.9%), HumanEval 3.50 (83.3%), MT-Bench 2.70 (56.6%) —
i.e. big on code/math, modest on free-form chat. Reference recipe (stock vLLM):

```
--speculative-config '{"method":"eagle3","model":"Inferact/MiniMax-M3-EAGLE3",
                       "num_speculative_tokens":3,"attention_backend":"FLASH_ATTN"}'
```

**Tested on this stack (2026-07-01) — it works but is a net loss; kept OFF.** The
reference runs stock vLLM + `FLASH_ATTN` + MXFP8; we run the b12x fork on SM120 with
**MSA sparse attention (`B12X_ATTN`) + NVFP4**. Empirically, driving it through the
`M3_EAGLE3` hook (below):

- **The draft's `attention_backend: FLASH_ATTN` crashes the b12x worker at init** —
  FLASH_ATTN isn't registered in this build; the draft can only use `TRITON_ATTN`
  (`Loading drafter model... Using TRITON_ATTN ... out of potential backends: ['TRITON_ATTN']`).
  Drop `attention_backend` from the spec-config and vLLM picks TRITON_ATTN for the
  draft (target stays `B12X_ATTN`); it then loads cleanly. Wiring is correct (shares
  target embeddings, keeps its own lm_head) and output is coherent (17×24 → 408).
- **Acceptance is excellent**: 80% (959/1197 drafted at spec=3), mean accept length
  ≈ 3.4 tok/forward (per-position 91/80/70%) — matching published EAGLE3 quality.
- **But single-stream is ~2× *slower*, not faster**: ~33–35 tok/s (code/count) vs the
  ~75–89 baseline. Cause, from the startup log: `CUDAGraphMode FULL_AND_PIECEWISE is
  not supported with spec-decode for B12XPagedAttentionBackend ... setting
  cudagraph_mode=PIECEWISE`. Losing FULL decode graphs reintroduces per-layer launch
  overhead across 60 layers × TP=4, and that penalty dwarfs the 3.4× acceptance gain.
  The draft is cheap and accurate; the b12x attention backend just can't run
  spec-decode under FULL graphs.

**Conclusion: keep EAGLE3 OFF on b12x** — a net loss for a *different* reason than
GLM's MTP (there, an expensive bf16 MoE draft; here, a fine draft killed by a
cudagraph downgrade). **On the stock MXFP4 stack this resolved exactly as
predicted** — stock's MSA backend keeps FULL decode graphs under spec-decode and
EAGLE3 is a 1.5–2.2× win there (see "Stock vLLM MXFP4 + EAGLE3" below); it is ON
in prod.

**The unblock is specific and not fundamental — FULL-cudagraph spec-decode is already
solved in b12x for the MLA family, just not for M3's MSA path.** `lukealonso/b12x`
[#11](https://github.com/lukealonso/b12x/pull/11) (merged 2026-06-09) added
`FULL_AND_PIECEWISE` MTP cudagraph capture for *compressed-MLA* (GLM DSA, DeepSeek-V4;
it raised the decode-split row threshold 64→256 so the `1+draft_tokens`-row verify
graph captures cleanly). But `B12X_ATTN` / `B12XPagedAttentionBackend` — M3's MSA
backend — still declares only `AttentionCGSupport.UNIFORM_SINGLE_TOKEN_DECODE`, so the
spec-decode verify batch can't be graph-captured and drops to PIECEWISE. The concrete
fix is a b12x change extending FULL-cudagraph support to `B12X_ATTN`'s multi-token
verify, mirroring #11 for compressed-MLA — **worth raising with lukealonso** (the b12x
author). (Separately, vLLM
[#33341](https://github.com/vllm-project/vllm/issues/33341) tracks FULL graphs for the
*drafter* itself — a smaller, additional overhead.)

The other route is the **stock-vLLM migration**, where a FLASH_ATTN target already has
FULL-cudagraph+spec-decode (plus the official M3+EAGLE3 integration, native tool
calling #45381, and dropping the opaque b12x image; gated only on stock's SM120 NVFP4
MoE kernels being coherent) — so the payoff comes from FLASH_ATTN as the target
backend, not from stock per se.

### Stock vLLM 0.24 on SM120 — tested 2026-07-01, loads but incoherent

Full serve test of `vllm/vllm-openai:v0.24.0` (torch 2.11+cu130, sm_120 in the arch
list) on the 4× RTX PRO 6000 node, TP=4. Everything initializes; the output is
deterministic garbage. What the test established:

- **Checkpoint must be `nvidia/MiniMax-M3-NVFP4`** (233 GB, the VL variant —
  `MiniMaxM3SparseForConditionalGeneration`, `hidden_act: swigluoai`,
  `language_model.model.*` weight keys). The `brandonmusic` quant we serve on b12x
  does **not** load on stock: its config mislabels `hidden_act: silu` and its weights
  are keyed `model.language_model.*` (packaged for the b12x loader).
- **`--block-size 128` is required.** M3's MSA backend declares
  `get_supported_kernel_block_sizes() == [128]` (one sparse block per KV page); the
  default (16) and 64 both die in KV-cache init with `ValueError: No common block
  size`. The selector only rounds down, so 128 is the single valid value.
- **On SM120 every GEMM falls back to Marlin weight-only.** The FlashInfer NVFP4
  kernels hard-gate to SM100 (`Using 'MARLIN' NvFp4 MoE backend out of potential
  backends: ['FLASHINFER_TRTLLM', 'FLASHINFER_CUTLASS', 'MARLIN']`, plus the
  `marlin_utils_fp4` "no native FP4" warning). No Blackwell FP4 tensor-core use at all.
- With that, init is clean: FLASH_ATTN attention, **FULL decode cudagraphs** (the MSA
  backend supports them on stock — the exact property b12x's `B12X_ATTN` lacks),
  17.6 GiB/GPU KV (416k tokens), 126 s engine init.
- **But generation is pure garbage**: at temperature 0, every prompt returns one
  repeated token (`지적했다`) to the token cap. Deterministic, prompt-independent —
  numerics corruption, almost certainly in the Marlin NVFP4 dequant path (the only
  untested kernel in play). Garbage decodes at ~71 tok/s single-stream, i.e. the
  Marlin path's *speed* would be acceptable if it were correct.
  (`--enforce-eager` bisect not run; logs on the node at `~/stock024-nvidia-test*.log`.)

**Verdict: stock 0.24 cannot serve M3 *NVFP4* on SM120.** The Marlin *NVFP4*
dequant path is broken; the FlashInfer NVFP4 kernels gate to SM100. What broke
the logjam (next section) was switching *quant format*: the 0.24 release notes
tie M3 support to **MXFP4** (#45896), and Marlin's MXFP4 path — the GPT-OSS
lineage, battle-tested on consumer Blackwell — is coherent.

### Stock vLLM MXFP4 + EAGLE3 — tested 2026-07-02, CURRENT PROD

The working stock recipe, now prod (managed by `install-minimax-m3.yml`;
`~dev/bin/minimax-m3-stock-setup.sh` then `~dev/bin/minimax-m3-stock-start.sh`):

- **Checkpoint `olka-fi/MiniMax-M3-MXFP4`** (221 GB): routed experts MXFP4,
  shared expert/attention MXFP8, built with qstream from MiniMaxAI's official
  MXFP8 release (GSM8K 92.9%, ppl 5.32 per the card). All four
  `--trust-remote-code` `.py` files verified byte-identical to the official
  `MiniMaxAI/MiniMax-M3-MXFP8`. Same recipe validated independently on this
  exact hardware by `0xSero/minimax-m3-sm120`.
- **One 3-line patch** on `compressed_tensors_moe_w4a4_mxfp4.py`: upstream
  drops the model's clamped-SwiGLU params, so `MarlinExperts` asserts
  `SWIGLUOAI_UNINTERLEAVE requires clamp_limit` at first forward. The fix
  threads `layer.swiglu_{alpha,beta,limit}` into `make_mxfp4_moe_quant_config`
  (all downstream plumbing exists). The setup script derives the patched file
  from the image with an anchored replace that fails loudly if upstream
  changes. **Candidate for an upstream vLLM PR.**
- Flags: `--block-size 128` (MSA page size, mandatory), `--kv-cache-dtype
  bfloat16`, `--linear-backend marlin` (+~2 tok/s), `--max-model-len 256000`,
  `--max-num-seqs 64`, `--limit-mm-per-prompt '{"image":0,"video":0}'`,
  `--default-chat-template-kwargs '{"thinking_mode":"enabled"}'`,
  `VLLM_MARLIN_USE_ATOMIC_ADD=1` (no measured change; harmless), TP=4 —
  each non-obvious one earned its place via a distinct failure (sharp edges
  below).
- **EAGLE3 ON by default** (`num_speculative_tokens 3`, no `attention_backend`
  in the spec-config). Stock's MSA backend keeps **FULL decode cudagraphs
  under spec-decode** — exactly what b12x's `B12X_ATTN` couldn't — so the 80%
  draft acceptance finally converts into wall-clock speed.
- Served ids: `brandonmusic/minimax-m3-nvfp4` (gateway compat — id predates
  the quant switch), `MiniMax-M3-NVFP4` (b12x-era alias), `minimax-m3`
  (generic).

Single-stream decode, temp 0, TP=4 (b12x NVFP4 baseline ~75–89 tok/s):

| config | tok/s |
|---|---|
| stock MXFP4, no spec-decode | 76.6–78.5 |
| + EAGLE3, free-form essay | 117 |
| + EAGLE3, technical explanation | 130 |
| + EAGLE3, code generation | **169** |

Known sharp edges (each was a real startup/serving failure at 256k):

- **Host-RAM OOM on 31 GiB nodes** (`--max-num-seqs 64` + cgroup caps): vLLM
  v1 preallocates *pinned* CPU buffers scaling with `max_num_seqs ×
  max_model_len` per worker — at 0.24's default seqs and 256k that's ~6 GiB
  anon per worker, and pinned pages can't swap, so the kernel OOM-kills a
  worker *silently* (only symptom: `Worker proc VllmWorker-N died
  unexpectedly`; check `journalctl -k` for `oom-kill`). Capping seqs at 64
  (b12x ran 4!) plus the start script's `--memory 26g --memory-swap -1`
  absorbs the startup spike.
- **GPU KV starvation from the vision tower**
  (`--limit-mm-per-prompt '{"image":0,"video":0}'`): the checkpoint is the VL
  variant, and the multimodal *encoder cache* scales with context — ~15 GiB
  GPU at 256k (budget 192k tokens, profiled with a max-size video), leaving
  only 2.7 GiB for KV ("estimated maximum model length is 54144"). We serve
  text-only; zeroing image/video skips the encoder entirely.
- **Streamed responses leak raw `<mm:think>` into `content`**
  (`--default-chat-template-kwargs '{"thinking_mode":"enabled"}'`): M3's
  default "adaptive" system prompt *literally contains*
  `<mm:think></mm:think>` as instructional text, tokenized as the real marker
  tokens. The reasoning parser's prompt back-scan hits that closer, decides
  reasoning already ended, and bypasses parsing for the whole stream (OWUI
  then renders tag spam; non-streaming is unaffected, which is why simple
  probes pass). `thinking_mode=enabled` prefills `<mm:think>` at the prompt
  end so the scan lands on a start marker and streaming parses correctly.
  Side effect: thinking is always on, not adaptive (per-request
  `chat_template_kwargs` still override). **Upstream vLLM bug** — `main`'s
  `is_reasoning_end` has the same last-marker semantics; worth filing.
- Reasoning + tool-call parsing use 0.24's built-in `minimax_m3` pair
  (`--reasoning-parser minimax_m3 --enable-auto-tool-choice --tool-call-parser
  minimax_m3`): `<mm:think>` goes to the reasoning field, `tool_choice: auto`
  returns structured `tool_calls` (verified streaming and non-streaming —
  Open-WebUI native tool calling works). Without these flags OWUI errors with
  "auto tool choice requires --enable-auto-tool-choice and --tool-call-parser
  to be set".
- The checkpoint's `generation_config.json` sets default sampling
  `temperature 1.0, top_p 0.95` (MiniMax's recommendation) for requests that
  don't specify their own.
- Load is ~5 min warm (no torch.compile pass, unlike b12x's ~15–20 min cold).

**Verify after any restart or image bump** with `./contrib/inference chat
minimax-m3` (from a machine with gateway config): it streams a completion and
then explicitly checks for think-tag leaks in streamed content and for working
streamed `tool_choice: auto` tool calls, exiting non-zero on failure.

Remaining watch-items: upstream the clamp fix (removes the patch); FlashInfer
MXFP4/NVFP4 kernels gaining SM120 (native FP4 tensor cores would lift
prefill/batch throughput above Marlin weight-only, which is decode-friendly
but compute-poor).

The plumbing is in place and inert: `M3_EAGLE3=1` on `minimax-m3-b12x-start.sh` mounts
the draft and injects the spec-config (default OFF, draft at
`/home/dev/models/MiniMax-M3-EAGLE3`); enable it only to re-test once `B12X_ATTN` gains
FULL-cudagraph spec-decode support (or on stock vLLM). **When re-testing, omit
`attention_backend` (or set `TRITON_ATTN`) — `FLASH_ATTN` hard-fails here.**

## 5. Register with the inference gateway

Once verified, add the backend so `local-ai-proxy` routes to it
(see [../operations/inference.md](../operations/inference.md)):

```bash
ycluster inference add http://nvN.xc:9211
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `unresolvable CDI devices nvidia.com/gpu=N` | podman 4.9.3 can't parse CDI 0.7.0 / `additionalGids` | downgrade spec to 0.6.0 + strip `additionalGids` (Prerequisites) |
| `Config not found for minimax-m3-vl` then engine init fails | wrong checkpoint (e.g. bullerwins) | use `brandonmusic/MiniMax-M3-NVFP4` (these `[ERROR]` lines are non-fatal warnings on the correct checkpoint) |
| `MSA sparse backend v1 is bf16-KV only (got fp8_e4m3)` | KV cache resolved to fp8 | append `--kv-cache-dtype bfloat16` |
| `invalid choice: 'bf16'` | wrong spelling | use `bfloat16` |
| Coherent-looking 200 but empty/`\0` content | broken cutlass NVFP4 MoE (wrong image) | use the b12x image, not the stock SGLang/0xSero one |
| `ValueError: No common block size for 16` (or 64) in KV-cache init | MSA requires KV page size 128 exactly | pass `--block-size 128` |
| One token repeated to the cap, temp 0, any prompt (stock vLLM) | Marlin **NVFP4** fallback incoherent on SM120 | use the **MXFP4** checkpoint (works via Marlin) — or b12x for NVFP4 |
| `AssertionError: SWIGLUOAI_UNINTERLEAVE requires clamp_limit` at first forward (stock + MXFP4) | compressed-tensors MoE path drops the clamped-SwiGLU params | run `~dev/bin/minimax-m3-stock-setup.sh` (derives the clamp patch); start script mounts it |
| `Worker proc VllmWorker-N died unexpectedly` during startup, no traceback | host-RAM OOM (kernel kill) — pinned CPU buffers scale with `max_num_seqs × max_model_len` | use the start script (`--max-num-seqs 64` + cgroup cap + unlimited swap); check `journalctl -k` for `oom-kill` |
| `To serve at least one request with the model's max seq len ... available KV cache memory (2.7 GiB)` at 256k | vision encoder cache scales with context (~15 GiB GPU) on the VL checkpoint | `--limit-mm-per-prompt '{"image":0,"video":0}'` (text-only serving) |
| Streamed `content` contains raw `<mm:think>` tags but non-streaming is clean | M3's adaptive system prompt embeds literal think markers, defeating the reasoning parser's prompt scan | `--default-chat-template-kwargs '{"thinking_mode":"enabled"}'`; verify with `./contrib/inference chat` |
