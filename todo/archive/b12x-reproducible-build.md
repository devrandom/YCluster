# Reproducible build of the b12x SM120 inference image

Supply-chain concern. The M3 NVFP4 deployment runs
`verdictai/minimax-m3-nvfp4-b12x:v1` — an opaque prebuilt Docker Hub
image (custom vLLM fork wheel + compiled SM120 NVFP4 MoE kernels + custom
torch `2.12.0+cu132` + patched NCCL + PR-stage FlashInfer/DeepGEMM) we
can't rebuild, audit, or pin against tampering. We don't want to run
unaudited third-party binaries on a GPU node long-term.

## Preferred resolution

**Drop b12x for stock vLLM, not rebuild the fork.** SM120 NVFP4 MoE
already works on stock (`nvidia/MiniMax-M2.7-NVFP4` runs on stock vLLM
`0.20.2` on the same RTX PRO 6000 / cc 12.0 arch), so once a stock
release carries #45381 (M3 model+parsers, merged) **and** the open SM120
MoE-kernel fixes (e.g. #43814), re-test `vllm serve` M3 on an nv node —
if coherent, retire the verdictai image entirely (no fork to reproduce,
native Rust tool calling for free).

The reproducible build below is the **fallback** until then.

## The whole recipe is public and commit-pinned

`local-inference-lab/blackwell-llm-docker` (`Dockerfile.vllm-b12x-cu132`
+ `build-black-benediction-b12xpr11-cu132.sh`, `PIN_SOURCE_COMMITS=1`)
derives from `nvidia/cuda:13.2.1-cudnn-devel` + pinned sources (vLLM
fork `bb6c5b7`, `lukealonso/b12x` PR#11 `d90d89c`, FlashInfer PR#3395
`b41aa8d`, DeepGEMM PR#324 `aced12c`, CUTLASS `d80a4e5`) + the official
torch wheel (torch is **not** a source build, so it's a ~1–2 h compile
of flashinfer/deepgemm/b12x/vllm, not a torch rebuild).

## Plan

Vendor the recipe + SHA-pinned source tarballs (PR refs can vanish),
ansible-ify the build (à la `incus-build-vllm-image`) with
`BUILD_BASE_IMAGE=1` so nothing derives from a `voipmonitor` binary,
push to `registry.xc`, repoint `minimax_m3_image`, drop the unused
`/opt/m3` wrapper, validate coherent output before cutover.

## Interim

Keep running the prebuilt image (rootless, non-sudo `dev`). Also the
lever to drop our reasoning-parser plugin and pick up the upstream tool
parser.

## Status

MOOT. We never built a reproducible b12x image — instead we dropped
b12x for stock vLLM `0.24`, per the todo's own "Preferred resolution".
The fork image, the b12x podman templates, and the local
`minimax_m3_reasoning_parser.py` plugin are now historical; current
prod state is in `docs/inference/minimax-m3-nvfp4-sm120.md` and the
b12x material was moved to `docs/archive/minimax-m3-b12x-stack.md`.

Refs:
- `docs/inference/b12x-reproducible-build-design-2026-06.md` (full design
  + pinned-SHA table)
