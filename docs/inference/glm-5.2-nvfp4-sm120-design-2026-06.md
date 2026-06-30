# GLM-5.2 NVFP4 on RTX PRO 6000 / SM120 — design

Status: **SERVING the full 744B `nvidia/GLM-5.2-NVFP4` on the whole 8-GPU node at
PP=2×TP=4** (see the 2026-06-26 update below — it supersedes the REAP/co-residency
plan in the back half of this note). It got there via a first attempt (on the REAP
variant) that crashed the node — the incident and its resolution are recorded
below because the fixes are load-bearing. GLM-5.2 overlaps almost the entire
MiniMax-M3 b12x stack; this note records what's reusable, what's different, the
incident, and how the rollout actually landed.

## Update (2026-06-26): full 744B nvidia checkpoint, PP=2×TP=4, MTP shelved

The phased plan below (stand up REAP-504B co-resident, decide on full later)
resolved in favor of the **full checkpoint, full box**. Current state, which
overrides the REAP-specific recipe/layout/plan sections that follow:

- **Checkpoint: `nvidia/GLM-5.2-NVFP4`** (full 744B). The REAP-504B was retired —
  REAP-pruning damages factual knowledge, and the full box was free to give GLM.
  `install-glm52.yml` points at it; served gateway id is `nvidia/GLM-5.2-NVFP4`.
- **Layout: PP=2×TP=4 across all 8 GPUs — NOT flat TP=8.** This is the headline
  reversal of the "shared TP=8 is the better default" recommendation below.
  Measured (`contrib/inference-bench.py`, single-stream, cold prefill): PP=2×TP=4
  gives **~2.2× prefill** (614→1370 t/s at 64k) and ~1.2× decode (33→39.5 t/s)
  over flat TP=8, validated by the smoke test (A 14/14, B 7/7 to 200k). Why: on
  this no-NVLink box TP=8's per-layer allreduce over all 78 layers is the
  bottleneck (GPUs sat comms-bound at 110–127 W/300 W); PP trades it for one
  point-to-point hop between two stages plus a TP=4 allreduce within a stage, and
  prefill has the token-parallelism to keep the pipeline full. `tp_size≥4` is
  mandatory (pure PP=8 hits the EP wall below), so PP=2×TP=4 is the layout.
- **EP is a dead end on b12x.** `--enable-expert-parallel` → `ValueError: NvFp4
  MoE backend 'B12X' does not support … ep_size=8`; the b12x kernel only has the
  TP MoE path, and the EP-capable cutlass/flashinfer backends NaN on SM120 (the
  reason we're on b12x at all). PP is the only working comms-reduction lever.
- **MTP is a measured net loss here — shelved** (`DISABLE_MTP=1` default),
  correcting the "MTP supported, ~60 tok/s" entry in the better-than-M3 section
  below. Two compounding blockers: (1) the MTP draft (`DeepSeekMTPModel`) doesn't
  implement vLLM's `SupportsPP`, so MTP forces PP=1 TP=8 — giving up the 2.2×
  prefill; (2) the checkpoint's MTP layer (78) is bf16/unquantized and b12x only
  does NVFP4, so the draft needs a slow generic kernel (`MOE_SPEC_BACKEND=triton`).
  Even working, decode came out ~26 t/s vs PP's ~39 despite ~85% acceptance — one
  256-expert bf16 triton draft layer costs ≈ a full 78-layer main forward, so it
  eats every token it saves. Revisit only if an NVFP4-quantized MTP checkpoint
  appears. (MTP weights *are* present in the nvidia checkpoint — the
  `GLM51_DISABLE_MTP` flag's origin was the REAP checkpoint genuinely lacking
  usable MTP, not these weights.)
- **`max_num_batched_tokens` 4096→16384 had zero single-stream effect** (614→613):
  the prefill ceiling is real per-token compute/comms, not scheduler chunking. The
  knob (and `MAX_NUM_SEQS`) only helps aggregate throughput under concurrency,
  which is untested. The start script exposes both as passthroughs anyway.

Launch is now just `~/bin/glm52-b12x-start.sh` with no overrides — the wrapper
defaults to this layout (`PP=2 TP=4 DCP=4`, all 8 GPUs, `MEM=110g` for the larger
warmup peak, MTP off). The `PP`/`DISABLE_MTP`/`MOE_SPEC_BACKEND`/`EXTRA_VLLM_ARGS`
knobs remain for A/B. The crash-safety machinery (fastsafetensors, checkpoint
dedup, `--memory` cap, indexer workspace) carries over unchanged and is still
load-bearing — read the incident section before touching it.

## First-attempt incident and resolution (2026-06-21)

First bring-up of `0xSero/GLM-5.2-504B` on the free 4-GPU slice cleared two boot
blockers and then **hard-panicked the node**, taking M3 (co-resident) down with
it. A power-cycle recovered it; M3 was restarted. A second attempt with the
fixes below came up clean and is what's serving now.

**Boot blockers found & fixed** (folded into `glm52-b12x-start.sh.j2`):
1. **NCCL `unhandled system error`** at `ncclCommInitRank`. The image bakes
   `NCCL_GRAPH_FILE=''` (empty), which crashes the patched NCCL; the M3
   entrypoint `unset`s it but we bypass that entrypoint. Fix: `--unsetenv
   NCCL_GRAPH_FILE`. Also: trust the image's baked NCCL/b12x env, don't override
   it (an earlier set of injected `NCCL_*`/`VLLM_*` vars from an unreliable
   source made it worse).
2. **`fastsafetensors` duplicate-key** (`model.layers.78.mlp.experts...`): the
   checkpoint redundantly stores its MTP (layer-78) tensors across several shards
   — 176 keys duplicated, plus a fully-orphan non-indexed shard. The index names
   one canonical copy per key (so the *default* loader never notices), but
   fastsafetensors scans the files and rejects the dups. Looks like a publisher
   packaging mistake. Fixed by deduping the checkpoint (keep the index-canonical
   copy, drop the rest) in `glm52-b12x-setup.sh`.

**The crash** (host-RAM OOM — confirmed by the fix):
- The node hard-froze ~3.5 min into the weight load and panicked
  (`Attempted to kill init, exitcode=0x0b` = init killed by **SIGSEGV**, *not*
  the SIGKILL a clean OOM-kill uses). Nothing was persisted: no OOM-killer line,
  no NVRM/Xid, no call-trace; `pstore`/`/var/crash` empty; the journal just cuts
  off.
- **Host RAM is the prime suspect.** The node has only **125 GiB** RAM. Watching
  M3's 214 GB default-loader load live, host RAM mostly sits ~21 GB but
  **transiently spikes to ~106 GiB used (~19 GiB free)** during one load phase.
  GLM is ~35% larger, so its equivalent spike would likely exceed 125 GiB and
  exhaust RAM. A *fast* allocation spike past the limit can hard-fail without a
  graceful OOM-kill — which fits the init-SIGSEGV and the absence of any
  OOM-killer log.
- The trigger was switching off `fastsafetensors` to the default loader (blocker
  2 "fix"): `fastsafetensors` streams direct-to-GPU and avoids that host-RAM
  spike, so dropping it for a model this size on a 125 GiB box was the cause.
- **Confirmed by the fix.** The successful retry on `fastsafetensors` held host
  RAM at **35–42 GiB** for the entire load (cap 80 GiB, never close) — vs the
  default loader's ~106 GiB spike. A b12x/DSA GPU-fault was never fully ruled out
  by logs, but the RAM trace makes memory the cause.

**Resolution (all in the scripts now):**
- **`fastsafetensors`** (streams direct-to-GPU) — the load-bearing fix.
- **Checkpoint dedup** in `glm52-b12x-setup.sh` so fastsafetensors accepts it
  (idempotent; the start script guards against an un-deduped checkpoint).
- **podman `--memory` cap** (default 80 GiB, verified enforced rootless on cgroup
  v2): a runaway is cgroup-OOM-killed (container), never init/the host. On the
  attempt before the dedup landed, this worked exactly as intended — GLM died
  *contained*, M3 and the host survived.
- Co-residency held: the successful bring-up ran with M3 up the whole time, host
  RAM never above ~42 GiB.

**Runtime gotcha — sparse-indexer workspace.** The b12x DSA indexer reserves a
scratch workspace at warmup and *locks* it (no runtime growth). The image default
(`VLLM_SPARSE_INDEXER_MAX_LOGITS_MB=512`) under-sizes it for long contexts, so
large agentic requests — opencode (big system prompt + tool schemas + history) is
the trigger we hit — crash the **engine core** with `Workspace is locked …
requires N MB … growth is not allowed` (≈960 MB needed vs ≈708 reserved). It's a
context-length problem, not tool-call parsing. The start script raises it
(`INDEXER_LOGITS_MB`, default 2048) — verified serving a ~24k-token request that
would previously have crashed. Trades a little KV-cache space; raise further for
very long contexts.

Node facts: 8× RTX PRO 6000 (96 GB), **125 GiB host RAM**, PCIe (no NVLink).

## What GLM-5.2 is

Z.ai/Zhipu's open-weight flagship (released 2026-06-13, MIT license, weights on
HF under `zai-org` ~06-17): a **~744B / ~40B-active MoE**, 1M-token context,
131K max output, with **IndexShare sparse attention** and **MTP** (multi-token
prediction) speculative decode. Coding/agentic focus rather than chat. Thinking
is on by default (DeepSeek-V4-style `reasoning_effort`; toggle off via chat
template kwargs).

## Why it overlaps M3 so heavily

It is the same class of system as M3 (see
[minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md)) and shares the exact
same serving stack:

- **Same kernel author and image family.** lukealonso (the b12x author behind
  our M3 kernels) publishes the GLM-5.2 NVFP4 checkpoints, and the serving image
  is the same `voipmonitor/vllm:black-benediction-…` b12x family we already run
  for M3 — it bundles `GlmMoeDsaForCausalLM` + `Glm4MoeMTPModel` + the SM120
  sparse-MLA kernel (`B12X_MLA_SPARSE`) + the ModelOpt NVFP4 MoE loader.
- **Same target hardware**: SM120 RTX PRO 6000 Blackwell (compute cap 12.0),
  NVFP4 MoE, PCIe (no NVLink).
- **Same host plumbing**: podman + nvidia CDI, rootless under the `dev` user
  (`setup-gpu-podman.yml`). The Ansible shape (`install-minimax-m3.yml` +
  setup/start templates) is near-cloneable.

So the marginal cost of adding GLM-5.2 is small — it's a parameterized clone of
the M3 deployment, not a new stack.

## Two ways it's *better* than M3 (and one worse)

**Better:**

1. **Tool calling works out of the box.** GLM-5.2 uses the **stock**
   `--tool-call-parser glm47`, and reasoning uses the **stock**
   `--reasoning-parser glm45` — both already in the b12x image's vLLM. That
   closes *both* of M3's gaps at once: no hand-injected reasoning plugin (M3
   ships `minimax_m3_reasoning_parser.py` via `--reasoning-parser-plugin`), and
   real tool calling (M3 has **none** — its XML format needs an upstream
   Rust-backed parser we can't backport). GLM-5.2 needs neither workaround.
2. **MTP speculative decode** is supported (`--speculative-config mtp`), ~60
   tok/s decode warm.

**Worse:**

- **The full 744B takes the whole 8-GPU node.** ≈372 GB NVFP4 weights → TP=8.
  It cannot co-run with M3 (which occupies 4 GPUs). See the variant choice
  below for the way around this.

Note also a **KV-dtype inversion vs M3**: M3's MSA backend rejected fp8 and
needed `--kv-cache-dtype bfloat16`; GLM-5.2's DSA backend is the **opposite** —
`--kv-cache-dtype fp8` is required and bf16 produces garbage. fp4 KV is
hardware-blocked on SM120 (the DSA fp4 indexer cache asserts SM100/datacenter
Blackwell). Don't copy M3's KV flag across.

## Hardware fit and the variant that unlocks a 4-GPU slice

The SM120 node has 8× RTX PRO 6000 (96 GB each, 768 GB total). M3 currently
occupies four of them; four are free.

- **Full `GLM-5.2-NVFP4` (744B)** ≈372 GB → **TP=8**, the whole node, displaces
  M3.
- **`0xSero/GLM-5.2-504B`** (public; 0xSero's `…-REAP-469B` is gated/private —
  401) — a REAP-pruned, router-KD-distilled variant for **4× RTX PRO 6000
  SM120**: ~292 GB on disk, same b12x image family. Runs on a **4-GPU slice**, so
  it can sit on the free GPUs **alongside** M3, or replace it. *Lighter* prune
  than the 469B → more experts retained → higher quality. REAP is still lossy
  (below full GLM-5.2), but it's the way onto a 4-GPU footprint, and being
  0xSero's own checkpoint it matches the validated recipe below. **This is what
  we pull.**

### Validated recipe (0xSero `launch.sh`, verbatim)

vLLM args (TP/DCP=4 is the 4-GPU-slice default; raise both to 8 for the shared
layout):
```
--trust-remote-code
--tensor-parallel-size 4 --decode-context-parallel-size 4 --pipeline-parallel-size 1
--quantization modelopt_fp4
--kv-cache-dtype fp8                       # fp8, NOT fp8_ds_mla; bf16 = garbage (opposite of M3)
--attention-backend B12X_MLA_SPARSE --moe-backend b12x
--enable-chunked-prefill --enable-prefix-caching --async-scheduling
--load-format fastsafetensors -cc.pass_config.fuse_allreduce_rms=True
--gpu-memory-utilization 0.95 --max-num-batched-tokens 8192 --max-num-seqs 2
--max-model-len 250000
--tool-call-parser glm47 --enable-auto-tool-choice --reasoning-parser glm45
--speculative-config mtp,3
--hf-overrides '{"index_topk_pattern":"FFFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSSFSSS"}'
```
Environment (note the b12x PCIe allreduce is *disabled* here — plain NCCL over
SYS instead, so the TP=8 latency tradeoff above is NCCL's, not b12x's):
```
NCCL_P2P_DISABLE=1 NCCL_P2P_LEVEL=SYS NCCL_SHM_DISABLE=0 NCCL_IB_DISABLE=1 NCCL_PROTO=LL,LL128,Simple
VLLM_USE_B12X_SPARSE_INDEXER=1 VLLM_USE_B12X_MOE=1 VLLM_USE_B12X_FP8_GEMM=1
VLLM_USE_V2_MODEL_RUNNER=1 VLLM_USE_FLASHINFER_SAMPLER=1 VLLM_ENABLE_PCIE_ALLREDUCE=0
HF_HUB_DISABLE_XET=1   # this node's egress can't reach the Xet CAS; the classic HF CDN works (~207 MB/s)
ENABLE_THINKING=1      # on by default; set 0 to disable reasoning
```

### Co-residency layout: disjoint TP=4+4 vs shared TP=8

When REAP-469B runs alongside M3 on the one node, there are two ways to lay the
two models across the 8 GPUs. This is a **temporal-sharing bet, not a throughput
law** — pick by expected concurrency, and confirm empirically at deploy.

- **Disjoint — M3 on GPUs 0–3 (TP=4), REAP on 4–7 (TP=4).** Each model owns 4
  GPUs permanently. Best when **both models are hot at once**: dedicated memory
  bandwidth and SMs per model, no cross-model collective/scheduler contention.
  On a typical 8-GPU PCIe box the 0–3 / 4–7 split usually lines up with the
  PCIe-switch / NUMA boundary, so each TP=4 group's allreduce stays switch-local
  (check `nvidia-smi topo -m`).

- **Shared — both models at TP=8 across all 8 GPUs, ~half the VRAM each.**
  Fits memory-wise (M3 ≈27 GB/GPU + REAP ≈39 GB/GPU of weights, leaving ~30 GB
  for KV on 96 GB cards). Best when usage is **bursty / one-model-at-a-time**:
  low-batch decode is memory-bandwidth-bound (you stream the active weights per
  token), so TP=8 doubles aggregate HBM bandwidth → roughly **halves per-token
  decode latency** for whichever model is active, while the idle model costs
  only its resident weights. Under genuinely *concurrent* load the two models
  split the same aggregate bandwidth and it converges back toward the disjoint
  case, minus extra sync.

The cost of shared TP=8 is **allreduce latency, not bandwidth**: at batch-1 the
collectives are KB-scale (latency-bound), but they sit on the serial per-layer
critical path, and 4→8 adds ring/tree hops over the PCIe-no-NVLink fabric. So
TP=8 trades a halved bandwidth term for a larger fixed latency term per layer.
Whether it nets to the full ~2x is empirical: the 0xSero REAP recipe reports
~60 tok/s at TP=4 — several-fold off its bandwidth floor — which hints fixed
per-token overhead (MoE dispatch, DSA, collective latency) is already
significant, so the bandwidth halving may apply to only part of the budget.

**Default:** for a low-concurrency cluster (usually talking to one model),
**shared TP=8 is the better default** — best-case interactive latency, graceful
when the other model is idle — but benchmark it against disjoint 4+4 at deploy,
since the per-token overhead could blunt the bandwidth win.

## Image — resolved: the M3 image we already run serves GLM-5.2

The "do we need a newer image" question is **closed: no.** 0xSero's `launch.sh`
pins the *same* image our M3 deployment already runs —
`voipmonitor/vllm:black-benediction-b12xpr11-vllmbb6c5b7-b12xd90d89c-fi3395b41aa8d-dg324aced12c-cu132-20260608`
(our `verdictai/minimax-m3-nvfp4-b12x:v1` is a retag of it). The GLM DSA model
code landed in the fork *before* GLM-5.2's weights dropped (from GLM-5.1 DSA
work), so the 06-08 image already registers it — **verified on the node**: its
vLLM model registry lists both `GlmMoeDsaForCausalLM` and `GlmForCausalLM`. So a
first launch needs **no new image** — GLM-5.2 REAP runs on the image already
present.

There is a newer GLM-5.2-specific line on Docker Hub
(`voipmonitor/vllm:glm52-dark-devotion-*`, 2026-06-18 → 06-21, all public) adding
GLM-5.2 DCP/MTP fixes (global-top-k DCP, MTP shard-draft, PR28/PR31). Treat it as
an **optional upgrade** to A/B if the 06-08 image underperforms — not on the
critical path.

## Plan (phased — evaluate REAP first, then decide on full)

Decision: **stand up REAP-504B co-resident first, benchmark, then decide whether
full 744B is worth displacing M3.** Write the playbook to support either layout;
defer the runtime GPU allocation to deploy time.

1. ~~Resolve the image.~~ **Done** — the M3 image already serves GLM-5.2 (see
   "Image — resolved" above). `glm52-dark-devotion-*` is an optional later A/B.
2. ~~Clone the M3 playbook~~ **Done** — `install-glm52.yml` +
   `glm52-b12x-{setup,start}.sh.j2` exist; checkpoint downloaded. **No
   reasoning-parser plugin** (glm45 is stock). `TP`/`GPUS`/`DCP` overridable.
3. ~~Stand up REAP-504B~~ **Done** — serving co-resident with M3 (GPUs 4–7,
   TP=4/DCP=4) after the crash-safe rework (see incident/resolution above).
4. **Validate** — coherence ✓ (17×24=408; bat-and-ball reasoned correctly, no
   NaN) and **tool calling ✓** (`get_weather{"location":"Paris"}` end-to-end
   through the gateway — the thing M3 can't do). Reasoning surfaces under the
   `reasoning` field. Remaining: full `inference-smoke-test.py` A/B/C — note B
   (needle) needs GLM's `enable_thinking` kwarg, not M3's `thinking_mode`, and C
   (vision) only applies if this checkpoint is multimodal.
5. ~~Wire into the gateway~~ **Done** — `ycluster inference add http://nvN.xc:9212`
   registered `0xSero/GLM-5.2-504B` (hot-reloaded, reachable via `inference.xc` +
   externally). A standalone runbook mirroring the M3 one is still TODO.
6. **Decide on full 744B**: if REAP quality is insufficient for the agentic/coding
   use case, stand up full `GLM-5.2-NVFP4` at TP=8 — which means displacing M3
   on the node. Defer this until the REAP benchmark is in hand.

## Endgame tie-in

GLM-5.2's reasoning/tool parsers (`glm45`/`glm47`) are **already upstream in
stock vLLM** — unlike M3, whose native support only landed in PR #45381
(2026-06-15). That reinforces the
[reproducible-build note's](b12x-reproducible-build-design-2026-06.md) preferred
endgame ("drop b12x for stock vLLM"): once a stock vLLM release carries the open
SM120 NVFP4 MoE-kernel fixes (e.g. #43814), `uv tool install vllm` could serve
GLM-5.2 natively with tool calling and no fork. The b12x image is the bridge,
not the destination — for GLM-5.2 even more clearly than for M3.

## Single-stream speedup options (2026-06-30 web survey)

Prompted by a "someone got 100+ tok/s on RTX 6000 Pro" report, against our
measured **~39.5 tok/s single-stream decode** at PP=2×TP=4. This section is
single-stream only — aggregate/concurrency tuning (`max_num_seqs`) is a separate,
untested axis and not covered here.

**Provenance of the "100 tok/s" number — it's an unverified forum rec, not a
measurement.** It traces to the `lukealonso/GLM-5-NVFP4` HF discussion (same b12x
kernel author we run): one poster got **31 tok/s** on 6× RTX 6000 Pro under
SGLang `--tp 2 --pp-size 3` (modelopt_fp4, flashinfer + flashinfer_cutlass, bf16
KV), another got **24 tok/s single-request** on 6 GPUs and hit `Pipeline
parallelism is not compatible with overlap schedule, speculative decoding` — and
the reply was *"upgrade to 8 and you can get 100 tokens per second."* No measured
8-GPU number backs it. Its entire premise is **8 GPUs + no PP so speculative
decoding turns back on** — i.e. exactly the TP=8+MTP experiment this doc already
ran and rejected above. So it adds no new information on its own; the decisive
variable is whether the speculative **draft** is efficient.

**Reframe: the impressive community numbers are aggregate, not single-stream.**
The other widely-cited figure — `lukealonso/GLM-5.1-NVFP4` discussion #4, "From
Doesn't Work to 641 tok/s" — is **641 aggregate at batch=128, where per-request
is only 7.6 t/s**; its **single-stream (batch=1) was 33 t/s** (TP=2/PP=3, dense
attn, `--enforce-eager`, no MTP) — *slower than our 39.5*. So the real
single-stream band on 6–8× RTX 6000 Pro for GLM-5.x is roughly **33–81 t/s**, and
our 39.5 sits squarely in it. **100+ t/s single-stream on the full 744B is very
likely not reachable on this hardware** — that target conflates batched aggregate
with per-stream. Treat ~60–85 t/s single-stream as the realistic ceiling and size
effort accordingly; if the actual need is *aggregate* throughput, raise
concurrency instead (separate axis, untested here).

The gap between our 39.5 and the realistic single-stream ceiling reduces mostly to
**an efficient spec-decode draft**, plus one config lever (DCP). Everything else
is already optimal here (NVFP4, fp8 KV, B12X sparse MLA, chunked prefill, prefix
caching, async scheduling), and the generic "go TP=8" advice is wrong for our
no-NVLink box (see the 06-26 update: TP=8 is comms-bound and *loses* 2.2×
prefill). Three real levers, ranked:

1. **An NVFP4-quantized MTP draft layer — highest leverage, stays on our stack.**
   This is the "revisit if an NVFP4 MTP checkpoint appears" condition from the
   06-26 MTP note — and as of this survey it has **partly appeared**. Today's MTP
   loss is *only* because our `nvidia/GLM-5.2-NVFP4` layer 78 ships bf16/unquantized
   (the stock ModelOpt quantizer rejects the nextn-layer experts) and b12x is
   NVFP4-only, forcing the slow 256-expert triton draft (~26 t/s even at ~85%
   acceptance). If layer 78 were NVFP4 the b12x draft kernel applies and MTP flips
   to net-positive. What the survey found:
   - **`lukealonso/GLM-5.1-NVFP4-MTP` exists** — same b12x author, explicitly "an
     experimental update … that includes a **quantized NVFP4 MTP model**" (84
     shards, dtypes incl. U8 = packed FP4). This is exactly the artifact we wanted,
     but for **5.1, not 5.2** — no `GLM-5.2-NVFP4-MTP` located yet.
   - **`0xSero/glm-5.2-sm120` already runs MTP on our exact stack** (`B12X_MLA_SPARSE`
     + `--moe-backend b12x` + `modelopt_fp4` + `--speculative-config mtp,3`) at
     **~60 t/s decode short / ~45 @ 64k–100k** — a direct counterexample to "MTP is
     a net loss on b12x." Caveats: it's the **REAP-469B** variant (smaller → cheaper
     draft) on **4 GPUs TP=4/DCP=4**, not our full 744B at PP=2×TP=4, and it uses
     `--kv-cache-dtype fp8_ds_mla` (we settled on plain `fp8`). So ~60 t/s is not a
     full-744B number — but it proves the mechanism.

   Action: (a) ask lukealonso for / watch for a `GLM-5.2-NVFP4-MTP`; (b) failing
   that, quantize layer 78 of our checkpoint with ModelOpt ourselves (pin the
   nextn experts to NVFP4 explicitly); (c) inspect 0xSero's REAP checkpoint for how
   its MTP layer is stored. Then re-run the MTP A/B (`DISABLE_MTP=0
   MOE_SPEC_BACKEND=b12x`). At ~85% acceptance a working NVFP4 draft is ~2–2.4× →
   **~80–95 t/s** realistic ceiling. (Open question: whether MTP can run under PP
   at all — `DeepSeekMTPModel` lacks `SupportsPP`, so this likely still forces TP=8
   and trades away prefill; bench both.)

2. **Benchmark SGLang head-to-head — biggest unexplored lever, but not a sure
   win here.** vLLM issue #26838 (RTX 6000 Pro) measured **SGLang 133 t/s vs vLLM
   78 t/s** on GLM-4.5-Air-FP8 at TP=4. SGLang's GLM-5.2 cookbook runs pure
   `--tp 8` with **EAGLE** spec (`--speculative-num-steps 5
   --speculative-eagle-topk 1 --speculative-num-draft-tokens 6`) and a **V2 spec
   engine** (`SGLANG_ENABLE_SPEC_V2=True` — "absolutely required" or it silently
   downgrades NEXTN→EAGLE and loads a second model copy as draft). EAGLE may
   sidestep the bf16-MTP-draft problem entirely. **Caveat that bites us
   specifically:** SGLang's NVFP4 MoE backends (flashinfer_cutlass / trtllm) have
   open garbage-output/OOM bugs on SM120 in early 2026 (flashinfer #2485, sglang
   #17955) — and we're on b12x *precisely because* cutlass/flashinfer NaN on
   SM120. So this needs an empirical bench on an nv node, with `zai-org/GLM-5.2-FP8`
   as a precision fallback (verify that checkpoint ships the MTP layer — the
   GLM-4.6-FP8 one reportedly doesn't).

3. **Lower DCP if we don't need extreme context — a pure-config A/B we can run
   today.** `DCP_SIZE` is already an exposed knob in the start script. 0xSero's
   SM120 ablation (REAP-469B, 4 GPUs) shows decode trades against DCP: **DCP=1
   ~81 t/s** (but max ctx ~131k, OOMs beyond), DCP=2 ~49, **DCP=4 ~60** (our
   setting, 250k ctx, ~3× concurrency). The table is non-monotonic (concurrency
   differs per row, so there are confounds) — so this is a "bench it on our full
   744B," not a guaranteed win — but DCP=4 exists to make **250k+ context** fit by
   sharding MLA KV along the sequence dim, and each decode step pays a cross-GPU
   KV gather for it. If our real context ceiling is well under 250k, a lower DCP
   may buy meaningful decode at the cost of max context (and KV-pool headroom).
   Zero-cost to try: `DCP=2` / `DCP=1` vs our `DCP=4`, same image, watch decode t/s
   and the max-context/OOM boundary. (Note: 0xSero uses `--kv-cache-dtype
   fp8_ds_mla`; we settled on plain `fp8` — worth re-confirming which the current
   b12x image wants, since it interacts with the DSA indexer cache.)

**Confirmed dead ends (don't re-litigate):** EP on b12x (already a `ValueError`);
`max_num_batched_tokens` tuning (zero single-stream effect, measured); fp4 KV
(hardware-blocked on SM120). A web survey's attempts to claim EP helps on PCIe or
that GLM-5 can't run on vLLM/SM120 did **not** survive verification.
