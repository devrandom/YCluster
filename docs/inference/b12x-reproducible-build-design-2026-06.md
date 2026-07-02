# Reproducible build of the b12x SM120 vLLM image — design

Status: **plan** (not yet implemented). **2026-07-02 update: largely mooted
for MiniMax-M3** — M3 prod now runs the *official* `vllm/vllm-openai:v0.24.0`
image (auditable, upstream-published) with a 3-line source patch derived
on-node; b12x is fallback-only there (see
[minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md)). The plan remains
relevant for **GLM-5.2**, which still serves on the b12x image family.

## Problem

The MiniMax-M3 NVFP4 deployment (see
[minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md)) currently runs a
prebuilt Docker Hub image, `verdictai/minimax-m3-nvfp4-b12x:v1`. It is an
opaque, deeply-patched binary stack from an unknown publisher: a custom vLLM
fork wheel with compiled SM120 NVFP4 MoE kernels (`_moe_C.abi3.so`), a custom
PyTorch `2.12.0+cu132`, patched NCCL, and FlashInfer/DeepGEMM built from PRs. We
cannot rebuild it, audit it, or pin it against tampering. Running unaudited
third-party binaries as a network-host container on a GPU node is a
supply-chain risk we don't want to carry long-term.

We don't accept running things we can't reproduce. This note is the path to a
build we control.

## What the image actually is

The `verdictai` image is **`voipmonitor/vllm:black-benediction-…-cu132-20260608`
re-tagged** with a thin `/opt/m3` entrypoint layer on top. We don't use that
layer — our launch script (`minimax-m3-b12x-start.sh`) overrides the entrypoint
args and supplies its own reasoning-parser plugin. So what we actually depend on
is the `voipmonitor/vllm:vllm-b12x-cu132` base, which is fully public and
reproducible.

The build recipe lives in **`local-inference-lab/blackwell-llm-docker`** (public)
— `Dockerfile.vllm-b12x-cu132` plus `build-black-benediction-b12xpr11-cu132.sh`,
which is named for and reproduces our exact image. It sets
`PIN_SOURCE_COMMITS=1` and hard-pins every source to a commit SHA matching our
running image's labels.

## The pinned recipe

| Component | Repo | Ref | Commit |
|---|---|---|---|
| vLLM | `local-inference-lab/vllm` (fork of `vllm-project/vllm`) | `dev/black-benediction` | `bb6c5b7351fceb9d524e0d43b957415ffefcb981` |
| b12x kernels (SM120 NVFP4 MoE/GEMM) | `lukealonso/b12x` | `refs/pull/11/head` | `d90d89c8353adabb56cc84bd3924ef811ef8d877` |
| FlashInfer (GDC for SM120) | `flashinfer-ai/flashinfer` | `refs/pull/3395/head` | `b41aa8dd2fb93c49b1c6134bd1953040f8089d51` |
| DeepGEMM | `deepseek-ai/DeepGEMM` | `refs/pull/324/head` | `aced12c2c8882a945c568ace9d4a7e5778aae410` |
| CUTLASS | `NVIDIA/cutlass` | `main` | `d80a4e53b52b42550659a8696dab32705265e324` |
| NCCL 2.30.4 (patched) | `local-inference-lab/nccl-canonical` | `canonical/cu132-nccl2304-amd-noxml` | — |
| PyTorch | official PyTorch wheel index (not source) | — | `2.12.0+cu132` |
| Base | `nvidia/cuda:13.2.1-cudnn-devel-ubuntu24.04` + cuBLAS 13.4.1, cuDNN 9.22, cuda-compat-13-2 595.71 | — | pin by digest |

Crucially, **torch is a wheel, not a source build** — only FlashInfer, DeepGEMM,
b12x, vLLM, and NCCL compile from source. That's a tractable ~1–2 h build on a
Blackwell node, not a multi-day PyTorch rebuild. Setting `BUILD_BASE_IMAGE=1`
rebuilds the intermediate bases too, so the whole image derives from the NVIDIA
CUDA base + pinned sources + the official torch wheel — **no `voipmonitor`
binaries in the chain**.

## Fork currency and the upstream M3 path

The vLLM fork (`dev/black-benediction`) branched from upstream `main` on
**2026-06-05** (merge-base `6a11d72d`, vLLM `0.11.2.dev`) and is already ~531
commits behind. Our pinned commit `bb6c5b7` is from **2026-06-08**; the branch
itself has since moved on (DFlash/MiMo work through 2026-06-12). The fork's ~48
commits are entirely Blackwell/SM120 b12x enablement (NVFP4/FP8 MoE+GEMM
kernels, sparse MLA/indexer, PCIe allreduce) plus DeepSeek-V4/DFlash and MiMo
FP4 support — **no MiniMax-M3-specific code**.

Critically, upstream landed **full native MiniMax-M3 support in PR #45381 on
2026-06-15** — ten days after our fork point. It adds the M3 model (MSA sparse
attention, vision tower, MTP), the reasoning parser we currently hand-inject as
a plugin (`vllm/reasoning/minimax_m3_reasoning_parser.py`), and a **Rust-backed**
tool parser (`vllm/tool_parsers/minimax_m3_tool_parser.py` +
`rust/src/tool-parser/src/minimax_m3.rs`). That timing is exactly why our image
has neither built in.

**Scope check — SM120 NVFP4 MoE is not categorically broken.** The
`nvidia/MiniMax-M2.7-NVFP4` model runs fine on the same SM120 arch (RTX PRO 6000,
compute cap 12.0) on a **stock vLLM release** (`0.20.2`, native `uv` install, no
b12x, tool calling on). So the b12x requirement is **M3-specific**, not a blanket
SM120 NVFP4 problem — M3 needs (a) SM120-correct NVFP4 MoE kernels for its
particular MoE shape (the stock *cutlass grouped-GEMM* path NaNs on it, while
M2.7's MoE uses a working backend), (b) the MSA sparse-attention backend, and
(c) M3 model code — none of which stock vLLM had at M3 deploy time.

## Resolution paths (preferred first)

1. **Preferred — drop b12x for stock vLLM.** Since SM120 NVFP4 MoE already works
   on stock (M2.7 above), the cleanest end-state is a stock vLLM release carrying
   both **#45381** (M3 model + parsers, merged 2026-06-15) and the open **SM120
   MoE-kernel fixes** — e.g. **#43814** ("Enable CUTLASS grouped GEMM (MoE) for
   SM_120/SM_121") plus the SM120 MSA/sparse-attention work. When such a release
   exists, `uv tool install vllm` and `vllm serve` M3 on an nv node, check for
   coherence; if it holds, we **retire the verdictai image entirely** and get
   native Rust tool calling for free — **no fork to reproduce**. Cheap to re-test
   whenever those PRs land. This is the real goal; the build below is the fallback
   until it arrives.
2. **Fallback — reproducible build of b12x + #45381.** Until stock can serve M3
   on SM120, the target is **b12x kernels + #45381**, not either alone (#45381
   alone still lacks the SM120 MoE-kernel fix and would NaN). Cleanest form: the
   b12x maintainers rebase onto / cherry-pick #45381, then we rebuild from pinned
   sources (below) — which also drops our reasoning plugin and gains Rust tool
   calling.

Things to watch: a stock vLLM release picking up #45381 **and** #43814 (→ path 1,
retire the fork); the b12x branch picking up #45381 (→ path 2).

### Upstream status of the two b12x halves (2026-06-30 survey)

b12x is two separable pieces, and they're upstreaming at very different rates.
This matters because **M3 needs only the first; GLM-5.2 needs both.**

- **SM120 NVFP4 MoE kernels — mostly landed in mainline.** Native NVFP4 MoE
  CUTLASS for SM120 is in stock vLLM (commits `e502098` "[Kernel] Add NVFP4 MoE
  CUTLASS support for SM120", `c0dfc89` "SM120/NVFP4 device guard + runtime SM
  dispatch"; `cutlass_scaled_mm_supports_fp4(120)` → True). The backend-*selection*
  bugs that forced a Marlin fallback (family check rejecting SM12.0) are being
  closed — **#33416 closed via merged #33417**, **#31085** tracks the rest. Our
  watched **#43814 is still open but is now the wrong PR to wait on**: it's *FP8*
  grouped GEMM (~7% over Triton), not NVFP4, and is blocked on CUTLASS #3280. The
  NVFP4 MoE path got there via other commits. Consistent with the M2.7 scope-check
  above (stock NVFP4 MoE already works on SM120).
- **DSA / sparse-MLA on SM120 — NOT upstream; this is the GLM-5.2 blocker.**
  `B12X_MLA_SPARSE` has no stock equivalent on SM120: sparse MLA in
  FlashMLA/FlashInfer is hand-written for **SM90a / SM100f only**, and SM120 hits
  `AssertionError requiring FlashMLA sparse kernel`. Open gap issue **#45317**
  ("DSA models — GLM-5.1 / DeepSeek-V3.2-family, `use_sparse=True` — cannot select
  any attention backend on SM121/SM120"). Only a slow Triton fallback exists
  (`VLLM_TRITON_MLA_SPARSE_ALLOW_CUDAGRAPH=1`, CUDA-graph-disabled by default).
  fp4 KV stays hardware-blocked (DSA fp4 indexer asserts SM100). The official vLLM
  GLM-5.2 recipe lists **only datacenter GPUs** (H200/B200/B300/MI300X, TP=8,
  vLLM 0.23.0) — no RTX 6000 Pro / SM120.

**Net:** M3 is close to a stock cutover (its sparse-attn is MSA, and the MoE half
is the blocker that's now mostly resolved — re-test stock on a node). **GLM-5.2
cannot leave b12x yet** — its DSA sparse-MLA half has no SM120 kernel upstream.
The GLM-5.2 issue to watch is **#45317**, not #43814. (Version caveat: these
threads reference vLLM 0.13–0.23, ahead of our pinned `0.11.2.dev` image — confirm
exact mainline state on an nv node before acting.)

## Build plan (fallback — resolution path 2)

Only needed until a stock vLLM release can serve M3 on SM120 (path 1 above).

1. **Vendor the recipe.** Mirror `blackwell-llm-docker` (and the source repos, or
   at least snapshot the PR-ref sources to SHA-pinned tarballs we host) into our
   control. PR refs (`refs/pull/N/head`) can move or disappear; the SHA is
   stable but GitHub serving it is not guaranteed — so pin the SHA **and** keep a
   tarball. Pin the CUDA base by digest and the torch wheel by exact version.
2. **Ansible-ify the build**, mirroring the existing `incus-build-vllm-image`
   pattern: a playbook that runs the build on a Blackwell node with
   `BUILD_BASE_IMAGE=1`, producing our image.
3. **Push to the internal registry** (`registry.xc`); repoint `minimax_m3_image`
   in `install-minimax-m3.yml` at it. Drop the `/opt/m3` wrapper — we don't use
   it.
4. **Validate** the rebuilt image serves M3 coherently before cutover: the
   sha256-verified checkpoint + a known-answer smoke test (e.g. `17 × 24 = 408`)
   and a streaming reasoning check (no `<mm:think>` leakage).

## Caveats / scope

- **Reproducible/auditable ≠ audited.** This buys provenance, tamper-pinning,
  and the ability to inspect and patch the sources — but the kernels are still
  bleeding-edge (b12x, PR-stage FlashInfer/DeepGEMM). The image's own numeric
  self-checks (the `minimax_m3 MoE numeric probe` lines in the logs) are a
  partial correctness signal, not a full audit.
- **"Bit-identical" reproducibility is not the goal** (apt/pip transitive
  versions drift, timestamps). The achievable, meaningful bar is **rebuildable
  from sources we host**.
- **Interim posture:** keep running the prebuilt image until our build is ready
  — it runs rootless as the non-sudo `dev` user and is otherwise contained.

## Bonus unlocked by building ourselves

Building from the vLLM fork is also the lever to close both M3 parser gaps —
upstream #45381 (2026-06-15) ships the built-in `minimax_m3` reasoning parser
(drop our injected plugin) and the Rust-backed tool-call parser (close the
tool-calling gap). See the "Fork currency" section above for the b12x-+-#45381
path, plus [minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md) and TODO.md.
