# GLM-5.2 NVFP4 on RTX PRO 6000 / SM120 — design

Status: **PAUSED** after a first bring-up attempt (2026-06-21) that hard-crashed
the GPU node (see incident below). The playbook + start script exist and the
checkpoint is downloaded; do not retry until the start script is made
crash-safe. GLM-5.2 overlaps almost the entire MiniMax-M3 b12x stack, so this
note records what's reusable, what's different, and the phased rollout.

## Status & first-attempt incident (2026-06-21)

First bring-up of `0xSero/GLM-5.2-504B` on the free 4-GPU slice cleared three
boot blockers and then **hard-panicked the node**, taking M3 (co-resident on the
same node) down with it. A power-cycle recovered it; M3 was restarted.

**Boot blockers found & fixed** (folded into `glm52-b12x-start.sh.j2`):
1. **NCCL `unhandled system error`** at `ncclCommInitRank`. The image bakes
   `NCCL_GRAPH_FILE=''` (empty), which crashes the patched NCCL; the M3
   entrypoint `unset`s it but we bypass that entrypoint. Fix: `--unsetenv
   NCCL_GRAPH_FILE`. Also: trust the image's baked NCCL/b12x env, don't override
   it (an earlier set of injected `NCCL_*`/`VLLM_*` vars from an unreliable
   source made it worse).
2. **`fastsafetensors` duplicate-key** (`model.layers.78.mlp.experts...`): this
   checkpoint duplicates an MTP-layer tensor across shards (64 safetensors vs 63
   in the index), which fastsafetensors rejects.

**The crash** (host-RAM OOM is the leading hypothesis, but unproven):
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
  spike, so dropping it for a model this size on a 125 GiB box was the likely
  cause. A GPU/kernel fault in the b12x/DSA path can't be fully ruled out (no
  logs), but memory is the strong lead.

**Before any retry — make it crash-safe:**
- Add a podman `--memory` cap to the start script so a runaway gets the
  *container* OOM-killed, never init/the host — and so a cgroup-OOM vs. other
  death disambiguates the cause.
- Restore `fastsafetensors` (streams direct-to-GPU) and fix the duplicate key by
  excluding the extra 64th shard, rather than the default loader.
- Bring GLM up **with M3 stopped** first (no production co-resident), watching
  host RAM + `dmesg`/Xid live, before attempting co-residency.

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
3. **Stand up REAP-504B — PAUSED after the 2026-06-21 node crash** (see incident
   above). Before retrying: make the start script crash-safe (`--memory` cap,
   restore `fastsafetensors` + exclude the duplicate shard), and bring it up
   **with M3 stopped first** (no production co-resident) watching RAM/dmesg —
   only then revisit the co-residency layout.
4. **Validate** with `contrib/inference-smoke-test.py` (A quality / B 250k needle
   / C vision) **plus a tool-calling check** — the thing M3 can't do. Confirm
   coherence (no NaN), reasoning folds, tools fire.
5. **Wire into the gateway** (`/cluster/config/inference/models/<name>`) and
   write a runbook mirroring the M3 one.
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
