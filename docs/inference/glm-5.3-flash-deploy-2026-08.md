# GLM-5.3-Flash on 2- or 4-GPU SM120 slices — deploy research & design

Status: **SERVING (Path A — 0xSero SGLang, NVFP4 @ TP4/EP4 on 4× RTX PRO 6000),
verified 2026-08-29.** Original analysis below; the *research/recommendation*
is now a live deployment (see [Deployment & verification](#deployment--verification-2026-08-29)
at the end of the plan). This note collects what we know about deploying
`GLM-5.3-Flash` on the RTX PRO 6000
(SM120) hardware we use for the other inference workloads, with the options
analyzed for both a **2-GPU** and a **4-GPU** slice. It is meant to be read
next to the GLM-5.2 note
([glm-5.2-nvfp4-sm120-design-2026-06.md](glm-5.2-nvfp4-sm120-design-2026-06.md))
and the DeepSeek-V4-Flash note
([deepseek-v4-flash-0731-dspark.md](deepseek-v4-flash-0731-dspark.md)), which
carry most of the shared SM120 / podman / b12x environment knowledge.

## TL;DR

- **4 GPUs is the right slice.** GLM-5.3-Flash is **320B total / ~18B active**,
  and on 96 GB RTX PRO 6000 cards the practical deployment is the **NVFP4**
  checkpoint at **TP=4/EP=4** (~45 GiB weights/GPU → big KV/KDA pool, ~1M
  context). A validated SM120 recipe (0xSero) serves it on **4× RTX PRO 6000
  at ~143–208 tok/s single-stream** (NEXTN/MTP speculative decode).
- **2 GPUs does not fit the NVFP4/FP8 checkpoints.** All NVFP4 variants are
  ≥181 GiB (> 2×89.4 GiB available on two 96 GB cards); native FP8 is ~306 GiB.
  The only viable 2-GPU path is the **EXL3/TR3 4-bpw** checkpoint
  (~164 GiB ⇒ ~82 GiB/rank at TP2) — validated on 2× RTX PRO 6000 with 262k
  context — but it carries a **non-OSI checkpoint license**.
- **Recommended 4-GPU recipe (in order of confidence):**
  1. **0xSero's locked SGLang bundle** (`lmsysorg/sglang:glm-5.3-flash`) +
     `LibertAIDAI/GLM-5.3-Flash-NVFP4` — the only published, digest-pinned,
     hardware-exact SM120 recipe with measured throughput.
  2. **voipmonitor b12x vLLM `glm53-flash-nvfp4-jovian` image** (2026-08-27) —
     same image family we already run for GLM-5.2/DS4, reuses all our
     podman/ansible/script conventions; **no public runbook yet, needs a
     qualification run**.
- If long- (1M) context or high concurrency on a 4-GPU slice is the goal,
  NVFP4@TP4 is the answer. If only 2 GPUs are available, expect 262k-context
  ceiling, a tight ~7 GiB/rank headroom, and a license caveat.

---

## What GLM-5.3-Flash is

Released by Z.ai 2026-08-27 (weights, MIT), previously served under the
pseudonym **"Ox Alpha"**. First **natively multimodal** model in the GLM-5
family (image + video in, standard OpenAI multi-part content).

| Property | Value |
|---|---|
| Architecture | 320B-total / **18B-active** MoE, **45 layers**, **8 of 288 experts**/token |
| Attention | **Hybrid**: KDA **linear** attention (recurrent conv-state) + **NoPE sparse MLA** (IndexPool/sparse) |
| Context | **1,048,576** tokens (IndexPool ~4× KV savings vs dense) |
| Spec decode | **MTP** (one next-N draft layer + adapter), official recipe: 5 spec tokens |
| Precision | Native **FP8**; community NVFP4 4-bit and EXL3 3/4-bit quantizations |
| Parsers | **`glm47`** tool-call, **`glm45`** reasoning — stock in vLLM/SGLang |
| Reasoning effort | **Low / High / Max** (default Max) — 3 real tiers, injected into the system prompt |

**Why it matters vs GLM-5.2:** the 8/288 MoE keeps *compute* (18B active)
tiny, but *memory* still scales with total params — so it is cheap to serve
on the 4-GPU slice we actually have, unlike the full 744B GLM-5.2 which needs
the whole 8-GPU node. The hybrid KDA linear + sparse MLA attention is a new
architecture (not DeepSeek-Sparse-Attention like 5.2), so it needs new kernels
(see SM120 notes below).

**Reasoning-effort note:** unlike GLM-5.2's two-state `high`/`max`, 5.3-Flash
has a real `low` tier. Pass `reasoning_effort` via `chat_template_kwargs` or
the top-level OpenAI field; any value other than `low`/`high` falls back to
`max`. Thinking is always on; skip a `max_tokens` budget or thinking will
silently eat it.

---

## Hardware fit — the 2-vs-4 GPU decision

Envelope (same as GLM-5.2/DS4): **RTX PRO 6000 Blackwell, 96 GB / card
(~89.4 GiB usable), PCIe, no NVLink**, 125 GiB host RAM. Slices are 2 or 4 of
these cards.

Checkpoint sizes (measured from HF, 2026-08-29):

| Checkpoint | Disk | Fits 4×96 GB? | Fits 2×96 GB? |
|---|---:|---|---|
| `zai-org/GLM-5.3-Flash` (native FP8) | ~306 GiB | marginal (~77 GiB/rank, little KV) | **no** (~153 GiB/rank > card) |
| `LibertAIDAI/GLM-5.3-Flash-NVFP4` | ~181.3 GiB | **yes** (~45 GiB/rank) | no (91 GiB/rank > 89.4 GiB) |
| `RedHatAI/GLM-5.3-Flash-NVFP4` | ~184.3 GiB | **yes** | no |
| `Blackfrost-AI/GLM-5.3-Flash-DERISKED-NVFP4` | ~191.0 GiB | yes (tighter) | no |
| `brandonmusic/GLM-5.3-Flash-tr3-4bpw` (EXL3 TR3) | ~163.7 GiB | yes | **yes** (~82 GiB/rank, ~7 GiB headroom) |

**Conclusion:**
- **4 GPUs** → NVFP4, TP=4/EP=4. Comfortable weight fit, leaves tens of GiB
  per card for the fp8 KV + KDA conv-state pool → long context + concurrency.
  Native FP8 *technically* fits at TP=4 but with almost no KV headroom and no
  published SM120 FP8 MoE servo; not worth it when NVFP4 fits so much better.
- **2 GPUs** → only EXL3/TR3 4-bpw fits (and tightly). NVFP4/FP8 physically
  cannot load (per-rank weights exceed card VRAM). This matches upstream:
  "nothing in between fits on two GPUs — Flash's low active count saves
  compute, not VRAM."

---

## Path A — 4 GPUs: NVFP4 @ TP=4 (recommended)

### A1. 0xSero locked SGLang bundle (validated reference)

**`0xSero/glm-5.3-flash-sglang-sm120`** — a digest-pinned, lock-pinned
SGLang deployment **exactly on our hardware** (4× RTX PRO 6000 SM120).

- Runtime image: `lmsysorg/sglang:glm-5.3-flash@sha256:3a97bd50…290` (six SM120
  compatibility patches **baked in**, no bind-mounted code).
- Model: `LibertAIDAI/GLM-5.3-Flash-NVFP4` @ `9e0d74e3…` (120 shards, 182 GiB),
  mounted read-only.
- Profile: **TP=4 / EP=4**, KV cache **`fp8_e4m3`** (3.79M tokens/rank, ~28 GiB),
  1M context; attention = **DSA backend** (`flashinfer_sparse_mla`)
  + **triton linear** (KDA); MoE runner = **`flashinfer_cutlass`** (SM120-native
  CUTLASS fp4); spec = **NEXTN(MTP), 5 steps, topk 1, 6 draft tokens**;
  parsers `glm45`/`glm47`; CUDA-graph decode ≤ batch 8.
- Measured (single stream, on 4× RTX PRO 6000): **~143 tok/s at `low`
  reasoning effort, ~208 tok/s at default (max)**.
- Boot: several minutes (182 GiB load + JIT + graph capture). Poll `/health`,
  not `/v1/models`.

Why `flashinfer_cutlass` and not `flashinfer_cutedsl`: CuteDSL hard-codes
`supported_major_versions=[10]` (datacenter sm100/103) in
`flashinfer/jit/moe_utils.py` and dies on SM120 with
`No supported CUDA architectures found for major versions [10]`. The
`cutlass` runner ships a dedicated SM120 module in this image and is the
validated one.

### A2. voipmonitor b12x vLLM (in-family, needs qualification)

Docker Hub `voipmonitor/vllm` (the same operator/images we run for GLM-5.2 and
DeepSeek-V4-Flash) has **GLM-5.3-Flash** tags already:

- `glm53-flash-nvfp4-jovian-vllmd2aa05e-b12xcad8fa5-fi1ac6942-cu133-torch213-20260827-r2`
- `glm53-flash-nvfp4-vllm479f2eef-b12x2fcf23a0-dflash2-fullgraph-20260828-r1`
- `glm53-flash-dflash2-mxfp8-vllm82bbab8-b12xfdbb504-…-cu133-torch213-20260828-r1…`
  (an **MXFP8** + DFlash2 variant — MXFP8 is the format that works on stock
  vLLM on SM120, cf. the M3 note)

These are cu133/torch-2.13 (same stack the DS4 host's driver 610 already
passes). **No public rtx6kpro runbook was located for GLM-5.3 as of writing**
(only GLM-5/5.1 pages exist), so treat A2 as "image exists, flags not yet
published — replicate A1's launch shape and qualify locally." Because b12x
already handles NVFP4 MoE + DCP + PCIe allreduce on our box, this is the
lowest-friction path to fold into our existing
`install-glm52.yml`-style playbook + `~dev/bin` script pattern if A1's SGLang
engine is not desired.

**Recommendation:** stand up A1 (proven, perf known) to get service + a
baseline; qualification-run A2 in parallel if we want to stay vLLM-only. If
the b12x vLLM path matches A1's throughput within noise, prefer it for fleet
consistency; otherwise keep A1.

**Host vs VM / driver-610 note:** neither GLM-5.3 path needs a GPU VM. The
GLM-5.2 step that ran in a VM was not because "GLM needs a VM" — it was
image-specific: that EXL3 TR3 image bundled CUDA 13.2.75, which hit CUDA
err 803 on the host's driver 610 (so the *in-VM* driver was downgraded to
580), plus a broken podman-4.9.3-CDI on that host that the VM sidestepped.
The native GLM-5.2 NVFP4 (our prod path) has always run on the host via
podman+CDI. A2 is **cu133/torch-2.13**, the exact stack the DS4
**driver-610 probe already passed** (torch 2.13.0 / CUDA 13.3 on driver
610.43.02, real matmul), and A1 is a fresh SM120-targeted cu13x SGLang
bundle — so plan to run GLM-5.3 on the host, no VM. Still do the same quick
CDI/short-run smoke on A1 that we ran for DS4 to confirm driver-610/CUDA
compat before standing it up.

---

## Path B — 2 GPUs: EXL3/TR3 4-bpw (the only 2-GPU option)

**`samuelcardillo/glm-5.3-flash-2x-rtx-pro-6000-blackwell`** — reproduced on
**2× RTX PRO 6000 96 GB** (exact same card), PCIe.

- Model: `brandonmusic/GLM-5.3-Flash-tr3-4bpw` @ `5ab363a8…` (~164 GiB,
  120 shards).
- Runtime image: `ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx@sha256:…ac5`
  (ExLlamaV3/TR3 4-bpw native path).
- Profile: **TP=2 + DCP=2** (`--decode-context-parallel-size 2
  --dcp-comm-backend ag_rs` over PCIe), adaptive **MTP K1–K5** + **ReplaySSM**,
  **NVFP4 MLA KV** (`--kv-cache-dtype nvfp4_ds_mla`), prefix caching,
  `--attention-backend B12X_MLA_SPARSE`, `--gpu-memory-utilization 0.95`,
  **262,144-token** request ceiling, `--max-num-seqs 16`,
  `--max-num-batched-tokens 2048`, vision (16 images, video off), parsers
  `glm47`/`glm45`.
- Env that matter: b12x activation + PCIe oneshot allreduce
  (`VLLM_ENABLE_PCIE_ALLREDUCE=1`, b12x backend, oneshot≤384KB), DCP top-k
  owner-merge set, EXL3 trellis knobs, `KV_FP8_ROPE=1`,
  `VLLM_NVFP4_MLA_DYNAMIC_SCALE=1`.
- Verified exact-token retrieval at **128k and 261.9k** context.
- KV pool: text-only ~734k tokens (≈2.8× conc @262k); vision ~545k (≈2.1×).

**Licensing caveats (relevant to our repo policy):**
- The EXL3 quantized checkpoint is **not MIT** — it's produced with
  **ShapleyMCG/TR3** under a **non-OSI, attribution-required license**
  (`other` on HF; explicitly grants no rights to "0xSero"). Review terms
  before use.
- `ghcr.io/tpurtell/…` is a third-party prebuilt image with two
  source-provenance gaps flagged by the author. Treat as an in-cluster Docker
  image (allowed by policy) but audit before promoting.

**When to pick Path B:** only when a 2-GPU slice is what's available and
262k context is acceptable. It caps at 262k (not 1M) and is a tight fit; for
the cluster, prefer freeing a 4-GPU slice for Path A.

---

## Cluster deployment recipe (Path A, podman/ansible shape)

Mirrors `install-glm52.yml` + `glm52-b12x-*.sh.j2` conventions (deployment-agnostic;
exact GPUs/ports live in host_vars, not here):

1. **Host prereqs**: podman + nvidia CDI (`admin/setup-gpu-podman.yml`) — already
   in place on our nv nodes.
2. **Checkpoint**: download `LibertAIDAI/GLM-5.3-Flash-NVFP4` to the model dir,
   verify HF shard sha256s (the 0xSero manifest lists the 4 core metadata
   hashes; still validate all 120 shards). Use the fast/direct-to-GPU loader
   to avoid the 125 GiB host-RAM spike trap we hit with GLM-5.2
   (`SAFETENSORS_FAST_GPU`/fastsafetensors) and keep the podman `--memory`
   cap.
3. **Runtime**: launch `lmsysorg/sglang:glm-5.3-flash@sha256:3a97bd50…`
   (SGLang A1) — or the voipmonitor b12x vLLM tag (A2) — on the 4-GPU slice.
4. **Register** with local-ai-proxy: `ycluster inference add http://<host>:<port>`
   (served name `glm-5.3-flash` / `LibertAIDAI/GLM-5.3-Flash-NVFP4` consistent
   with the `install-glm52.yml` convention that one served name is the
   canonical id).
5. **Verify**: `/health` (SGLang) or a real completion — never trust a bare
   `/v1/models` 200. Then run our `./contrib/inference smoke <model>` harness;
   add a new GLM-5.3 suite once serving.

## Key flags / non-obvious knobs

- **`--kv-cache-dtype fp8` (A1) / `nvfp4_ds_mla` (B)** — fp8/NVFP4 MLA KV
  works for this model (unlike GLM-5 where bf16 was mandatory on SM120). Do
  *not* copy GLM-5.2's exact KV value blindly; follow per-recipe.
- **`--tool-call-parser glm47 --reasoning-parser glm45 --enable-auto-tool-choice`**
  — stock; tool-calling + reasoning both work out of the box (no plugin).
- **MTP/NEXTN spec** is the throughput lever: ~2× single-stream decode over no
  draft. A1: NEXTN 5 steps/6 drafts; B: adaptive MTP K1–K5. On GB10 the
  DFlash2 drafter is ~2.15× over MTP-4, but that's SM121-specific and not
  validated on SM120.
- **Reasoning effort**: `low` is substantially faster (~143 vs ~208 tok/s in
  A1) at real quality cost; use for interactive/chat, `max`/default for hard
  agentic.
- **`max_tokens` includes reasoning tokens** — with thinking on, a small
  `max_tokens` yields empty `content` while the budget burns. Omit it for
  thinking-on requests, or pass
  `chat_template_kwargs: {"enable_thinking": false}`.
- **KDA conv-state layout pins** (PDL/DS) matter for correctness on SM12x; the
  A1 image bakes them — don't strip the image's baked env.

## Scheduler fairness

SGLang schedules prefill and decode in separate batches here; mixed chunk is
rejected with speculative decoding. If generation slows while a long prefill is
active, see [prefill-decode-fairness.md](prefill-decode-fairness.md). The closest
controls are `--prefill-decode-interval` (default `0`, disabled),
`--chunked-prefill-size`/`--max-prefill-tokens` (both bound the prefill batch),
and `--prefill-max-requests`. Keep `--max-running-requests` equal to
`--cuda-graph-max-bs-decode`; exceeding the captured graph shape is an
eager-fallback cliff.

## Gotchas / troubleshooting

| Symptom / risk | Cause | Handling |
|---|---|---|
| `No supported CUDA architectures found for major versions [10]` | CuteDSL MoE gated to sm100/103 | use `flashinfer_cutlass` MoE runner (A1 images) |
| Host-RAM OOM during weight load | 182 GiB shards + default loader on 125 GiB host | fast/direct-to-GPU loader + podman `--memory` cap (GLM-5.2 incident) |
| NVFP4/FP8 "doesn't fit" on 2 GPUs | per-rank weights > 89.4 GiB card VRAM | it cannot fit; use EXL3 4-bpw (Path B) or a 4-GPU slice |
| Empty `content` on short `max_tokens` | thinking-on consumes the budget | raise/omit `max_tokens` or disable thinking |
| `/v1/models` 200 but engine dead | config served before engine ready | poll `/health` (SGLang) / real completion |
| fp8 MLA KV NaN / garbage | FlashInfer/MLS backend version too old | require FlashInfer ≥ 0.6.18 (baked into the pinned images) |
| SPDX/policy | EXL3 checkpoint = non-OSI; tpurtell image has provenance gaps | only use Path B after license review |

## Deployment & verification (2026-08-29)

Path A is live and serving through the cluster gateway. Deployment-specific
facts (exact GPUs, port, served name) live in the private per-host vars /
`install-glm53-flash.yml`, not here.

- **Recipe:** 0xSero-locked base `lmsysorg/sglang:glm-5.3-flash@sha256:3a97bd5…`
  + the six baked SM120 patches, rebuilt locally as
  `localhost/glm53-flash-nvfp4-sglang:sm120-20260829`. Model
  `LibertAIDAI/GLM-5.3-Flash-NVFP4` @ `9e0d74e3…` (182 GiB, 120 shards), mounted
  read-only at `/model`.
- **Sizing (`/get_server_info`):** `tp_size=4`, `ep_size=4`, `context_length=1048576`,
  `kv_cache_dtype=fp8_e4m3`, `served_model_name=glm-5.3-flash`.
  **Total token pool (all concurrent requests combined): `max_total_num_tokens =
  3,739,072`** (~3.74M; @2k-prompt/512-decode cols that is on the order of hundreds
  of concurrent sessions), `max_running_requests=8`.
- **Host software:** podman + nvidia CDI. The GPU node runs a source-built
  **podman 5.8.0** (`admin/install-podman.yml`) — apt's 4.9.3 broke CDI here,
  so the newer podman is a required prereq for this SGLang container.
- **Registration:** served as `glm-5.3-flash` via local-ai-proxy; `/v1/models`
  healthy, listed in `ycluster inference ls`. Gateway `ycluster inference status`
  reports the backend healthy (0 down).
- **First-request latency caveat:** the SGLang engine JIT-compiles Triton kernels
  *after* serving starts (e.g. `chunk_kda_fwd_kernel_inter_solve_fused`,
  `_recompute_w_u_fwd_kernel`), so the very first completion after a fresh boot has
  a multi-second TTFT (~50 s in our smoke). Bake the engine warm before any
  latency-sensitive use (the `inference` bench/batch tools run a warmup round).

### Smoke test

`inference chat glm-5.3-flash` (via the gateway): real completion generated,
`reasoning` streamed separately (no `<mm:think>` leak in `content`, tool-call
stream produced a structured `get_weather` call). First-request TTFT ~54 s
(cold Triton JIT); steady-stream ~47 tok/s at default reasoning on a short
interactive prompt.

Note: the smoke harness's tokenizer round-trip uses `content`/`token_ids`
field names and reported a mismatch against this SGLang backend, which instead
expects `prompt`/`tokens`. Verified against the real schema the backend is
fine: `tokenize("hello world") -> [14978,1879]`, `detokenize([14978,1879])`
round-trips back to "hello world".

### Throughput (decode-heavy, `inference bench`, thinking off)

`inference bench glm-5.3-flash --lens 128,2048,16384 --gen 256 --repeats 3`
(streaming, `ignore_eos`, temp 0, warmup on):

| Prompt tokens | TTFT | Prefill t/s (med) | Decode t/s (med [min-max]) |
|---:|---:|---:|---:|
| 130 | 0.28 s | 458 | 153.2 [112.7–159.7] |
| 1,738 | 0.66 s | 2,631 | 159.4 [129.3–165.6] |
| 13,762 | 3.82 s | 3,603 | 154.2 [141.7–161.7] |

Prefill throughput scales to multi-k t/s; decode is stable ~150–160 tok/s
single-stream with thinking off. This squares with 0xSero's published ~143–208
tok/s single-stream (reasoning-effort dependent).

### Concurrency (gateway batch, `inference batch`)

`inference batch glm-5.3-flash --concurrency 1,2,4,8 --prompt-tokens 2048
--gen 256 --rounds 3` (0% prefix-cache; show aggregate decode):

| Concurrency | Requests/s | TTFT (med) | Per-req decode t/s | **Aggregate decode t/s** |
|---:|---:|---:|---:|---:|
| 1 | 0.56 | 0.26 s | ~166 | ~143 |
| 2 | 0.91 | 0.40 s | ~145 | ~225 |
| 4 | 1.44 | 0.41 s | ~114 | ~373 |
| 8 | 2.04 | 0.48 s | ~85 | **~531** |
| 16 | 2.14 | 1.84 s | ~79 | **~553** |
| 32 | 2.06 | 5.89 s | ~72 | **~530** |

Aggregate decode throughput hits its ceiling at the `max_running_requests=8`
engine limit: it plateaus at ~530–553 tok/s from conc 8 through conc 32 (all
batches 0% prefix-cache, 100% agreement, no errors). Above conc 8 the extra
requests queue at the engine, so **Requests/s** flatlines (~2.1) and TTFT
grows (0.48 s @ conc 8 → 1.84 s @ conc 16 → 5.89 s @ conc 32), while per-request
decode drifts down as batches grow (166 → 72 t/s). To raise aggregate
throughput much past ~550 tok/s you'd need a higher
`--max-running-requests` (trade larger scheduling batches / higher TTFT) or a
second backend slice; TTFT stays sub-second only out to ~conc 4.

### Long-context smoke (`inference smoke`, 200k target)

`inference smoke glm-5.3-flash A B C --max 200000` through the gateway:

- **Suite A (quality, temp=0, thinking on): 14/14**, 0 reasoning leaks.
- **Suite C (multimodal): 2/2** — "42 in a red circle" and APPLE/BANANA both
  parsed correctly, so the vision path is live.
- **Suite B (long-context needle, up to ~168k actual prompt tokens): 6/7.**
  Retrieval pass at 2k/16k/64k/128k mid-context and at the 200k target at
  50% and 90% depth (all `fin=stop`). The one miss was **200k@10%**:
  `fin=length` at 44.8 s — the generation hit its length budget before it
  could state the needle. That is the "thinking consumes `max_tokens`"
  behavior from the gotchas table, not a retrieval failure: the same
  prompt length retrieved the needle at 50% and 90%.

Note the harness capped the 200k run to 167,870 actual prompt tokens; the
engine's 1M ceiling leaves headroom to push a larger needle later.

### Disk / memory check

NVFP4 checkpoint is **182 GiB** staged under the model dir; node root has
~200 GiB free at verify time (plenty for the checkpoint + build context).
Weights load direct-to-GPU with the fast loader (see host-RAM OOM gotcha above).

## Known incident: ~4 min decode past apparent response completion

An incident against the SGLang A1 deployment exposed a termination bug worth
re-testing after any image upgrade. A local trace proxy (full request/response
capture with per-request IDs) confirmed the sequence:

- The incident request was sent at T+0 and prefilled as 128 new tokens on a
  ~240k cached prefix; the gateway logged it as one 326s streaming request.
- The engine decoded ~10.8k tokens in the first ~70s, then the stream stalled
  somewhere between the engine and the client. The engine did **not** stop: it
  decoded ~71k more tokens over the next ~4m13s (`#running-req: 1`
  continuously, token pool 251k → 322k), with no abort/cancel/disconnect
  entries.
- At exactly T+301s the client gave up (looks like a 300s client timeout) and
  retried the identical turn (`request_kind: turn`, same turn/prompt-cache
  IDs). The retry completed in ~7s with 50 output tokens — **while the
  abandoned original was still decoding**, confirming the engine was healthy
  and concurrent.
- The trace proxy has no completion record for the original request at all —
  its streaming handler apparently never observed end-of-stream — while the
  gateway logged the original connection closing only when the engine finished
  the sequence (~4min after the visible output had stopped advancing).

Net effect: ~71k tokens (~4 GPU-minutes) were generated for a request whose
client had already timed out and moved on, because nothing in the chain
propagated the client-side abort to the engine. Follow-ups:

- Reproduce with a client that disconnects mid-stream and verify the engine
  aborts the sequence; if not, open an issue upstream (SGLang / the 0xSero
  bundle) with this signature (decode continues, `#running-req` stays 1, no
  abort log, long after client abandonment).
- Add request-ID correlation across gateway/proxy/engine logs so lingering
  requests are trivially diagnosable.
- Check every hop's disconnect handling: a logging proxy that only detects
  client disconnects when it writes a chunk will never notice if it is itself
  blocked reading a stalled upstream.

Attribution notes from the same incident window:

- A concurrent ~78k-token cache-miss prefill (chunked at 4096) sustained
  ~3.8–3.9k tok/s and took ~21s. It briefly competed with an in-flight decode
  but was **not** the main stall.
- Prefilling a small suffix on top of a ~240k-token cached context measured as
  low as ~15–90 tok/s. This is not a slow cache lookup: on a cache hit the
  prefix KV is reused directly. The cost is that every new token must attend
  over the entire cached prefix, so attention work and KV reads scale with
  `new_tokens × context_len`. Small suffix chunks (e.g. 128–256 tokens) cannot
  amortize per-step overhead, which is worse because **prefill CUDA graph is
  disabled for this model** (incompatible with KDA/linear attention). Large
  cache-miss chunks (~4k tokens) amortize much better and reached ~3.8–3.9k
  tok/s in the same window.

## Open questions before committing

- Confirm the **voipmonitor b12x vLLM `glm53-flash-nvfp4-jovian`** launch flags
  (no public runbook yet) and A/B it head-to-head with A1 on the same 4-GPU
  slice — prefer it for fleet consistency if within noise.
- Confirm **MTP-vs-DFlash2** on SM120 (validated on GB10 only).
- Confirm long-context (IndexPool) **quality** at ≥1M with the smoke harness
  (a 200k-target smoke passed 6/7 on retrieval, one `fin=length` miss; the
  engine is verified serving 1M context, and the 125 GiB host-RAM +
  fast-loader question is resolved — see disk check above).

## References

- Z.ai release: https://z.ai/blog/glm-5.3-flash · vLLM recipe:
  https://recipes.vllm.ai/zai-org/GLM-5.3-Flash
- Model cards: `zai-org/GLM-5.3-Flash` (FP8) · `LibertAIDAI/GLM-5.3-Flash-NVFP4` ·
  `RedHatAI/GLM-5.3-Flash-NVFP4` · `brandonmusic/GLM-5.3-Flash-tr3-4bpw`
- 0xSero SM120 SGLang bundle: `github.com/0xSero/glm-5.3-flash-sglang-sm120`
- 2× RTX PRO 6000 EXL3 bundle: `github.com/samuelcardillo/glm-5.3-flash-2x-rtx-pro-6000-blackwell`
- In-family images: Docker Hub `voipmonitor/vllm` `glm53-flash-*` tags
- Related: `glm-5.2-nvfp4-sm120-design-2026-06.md`,
  `deepseek-v4-flash-0731-dspark.md`
