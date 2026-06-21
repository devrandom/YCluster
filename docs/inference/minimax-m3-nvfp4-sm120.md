# MiniMax-M3 NVFP4 on RTX PRO 6000 (SM120)

Serving MiniMax-M3 (NVFP4 quant, 428B total / 23B active multimodal MoE,
MiniMax Sparse Attention, up to 1M context) on a node with 4× NVIDIA RTX PRO
6000 Blackwell (compute capability 12.0, "SM120").

## TL;DR

The stock SGLang/cutlass NVFP4 MoE path is **broken on SM120** — it loads and
serves but produces NaN logits → garbage tokens. The working path is
lukealonso's **b12x** kernel family (CuTe DSL NVFP4 grouped-GEMM for SM120),
packaged in the `verdictai/minimax-m3-nvfp4-b12x` image, paired with the
`brandonmusic/MiniMax-M3-NVFP4` checkpoint, with the KV cache forced to
`bfloat16`. Runs as a rootless-podman container under the `dev` user, TP=4,
OpenAI-compatible on port 9211.

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
