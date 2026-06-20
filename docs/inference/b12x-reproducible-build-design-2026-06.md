# Reproducible build of the b12x SM120 vLLM image — design

Status: **plan** (not yet implemented). Motivation, recipe, and the build plan
for replacing the third-party prebuilt inference image with one we build from
pinned sources and host ourselves.

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

## Build plan

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

Building from the vLLM fork is also the lever to:
- Cherry-pick upstream's built-in `minimax_m3` **reasoning** parser and drop our
  injected plugin.
- Pick up the upstream `minimax_m3` **tool-call** parser (Rust-based, absent from
  this image build) and close the tool-calling gap — see
  [minimax-m3-nvfp4-sm120.md](minimax-m3-nvfp4-sm120.md) and TODO.md.
