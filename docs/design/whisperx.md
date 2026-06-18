# WhisperX on AMD Strix Halo (Ryzen AI Max+ / Radeon 8060S, gfx1151) — Container Strategy, May 2026

## TL;DR

- **Sweet spot is CTranslate2 v4.7.1's official `rocm-python-wheels-Linux.zip` running on a `rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0` base, with `HSA_OVERRIDE_GFX_VERSION` UNSET.** AMD's ROCm 7.2.2 apt repo (noble pocket) ships rocBLAS Tensile kernels for gfx1151, and OpenNMT/CTranslate2 PR #1989 ("Introduce AMD GPU support with ROCm HIP", author `sssshhhhhh`, merged Feb 3 2026 as squash commit `68917da`) bakes gfx1151 directly into the released ROCm shared library — so a CTranslate2 source build is no longer strictly required for gfx1151 (verified by `strings` inspection of the cp312 .so by `nabe2030/faster-whisper-rocm-strix-halo`).
- **For full WhisperX (faster-whisper transcription + wav2vec2 alignment + pyannote.audio diarization) the only end-to-end verified containerized Strix Halo reference today is `ghecko/whisperx-rocm-docker` (Mar 2026, `rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.10.0`, CTranslate2 source-built with `-DCMAKE_HIP_ARCHITECTURES=gfx1151`, pyannote.audio ≥4.0, WhisperX from git).** It pre-dates the v4.7.1 wheel and still source-builds CT2; a modernized variant that drops the source build (Variant A below) is the recommended path.
- **Benchmarks: faster-whisper large-v3 fp16 hits ~11.5× realtime on JFK 11 s and 6.56× realtime on a 30 min 44 s Japanese seminar that transcribed in exactly 4 min 41 s — 573 segments processed without any memory fault or VRAM leak, with engine usage peaking at 96%** (nabe2030, GMKtec EVO-X2, CT2 v4.7.1 wheel + ROCm 7.2.2). The earlier davidguttman/whisper-rocm recipe (ROCm 6.4.3 + CT2 v3.23.0 source-built) reports ~9× realtime large-v3 — both ~2× faster than whisper.cpp HIP on the same hardware. WhisperX adds wav2vec2 alignment + pyannote diarization on top.

## Key Findings

### 1. CTranslate2-rocm — upstream ROCm support landed Feb 2026

- **Upstream CTranslate2 now ships ROCm wheels.** `OpenNMT/CTranslate2` PR #1989 (https://github.com/OpenNMT/CTranslate2/pull/1989) was merged on Feb 3 2026 (commit `68917da`, pushed by `jordimas`). Release v4.7.0 (Feb 3) and v4.7.1 (Feb 4 2026; signed commit `226c95d9`) attach the asset `rocm-python-wheels-Linux.zip` (~284 MB) at `https://github.com/OpenNMT/CTranslate2/releases/download/v4.7.1/rocm-python-wheels-Linux.zip`. PR body: built initially against ROCm 7.1.1, finalized at **ROCm 7.2**. The author's verbatim follow-up: *"Currently building for rocm 7.2 … install ctranslate2 whl from github actions artifacts."* v4.7.1's release notes mention only the Windows build fix (#2007); they do not enumerate gfx targets.
- **gfx1151 IS in the binary.** Third-party `strings` inspection of the cp312 .so by nabe2030 reports targets: `gfx803 gfx900 gfx906 gfx908 gfx90a gfx942 gfx950 gfx1030 gfx1100 gfx1101 gfx1102 gfx1150 gfx1151 gfx1200 gfx1201`. Coverage spans GCN3 through RDNA4.
- **The .so dynamically links to system ROCm** (`libamdhip64.so.7`, `librocblas.so.5`, `libhipblas.so.3`, `libhipblaslt.so.1`) under `/opt/rocm/lib/`, so the system ROCm install provides the Tensile kernel binaries for gfx1151. No bundled ROCm runtime; install order is strict (system ROCm first, then wheel).
- **Maintained ROCm forks (now superseded for gfx1151, still useful as references)**:
  - `arlo-phoenix/CTranslate2-rocm` — first widely-used HIP'd fork; explicitly thanked in PR #1989 body and the basis for the upstream merge.
  - `paralin/ctranslate2-rocm` — fork that documents `Memory access fault by GPU node-1` on long audio for gfx1101 (RX 7600 family). Not reproduced on gfx1151 in nabe2030's 30 min test on the v4.7.1 wheel + ROCm 7.2.2.
  - `pigeekcom/wyoming-faster-whisper-rocm` Docker image (last push ~Oct 2025, tag `rocm7.0-strix`, image size 9.1 GB) advertises explicit Strix Halo support: *"AMD ROCm build of Wyoming Faster Whisper with a precompiled CTranslate2 ROCm fork … targeting gfx900–gfx1151 including Strix Halo (AMD Ryzen™ AI Max+ 395 with Radeon 8060S GPU)"*. The linked `github.com/pigeek/CTranslate2-rocm` repo could not be independently verified in this session — treat as last-resort fallback. Useful for Home Assistant Wyoming protocol, not OpenAI-API-compatible.
  - `kprinssu/CTranslate2-rocm` — earlier ROCm 6.2 / arlo-phoenix derivative.
- **davidguttman/whisper-rocm** — published recipe targeting Radeon 8060S (gfx1151) on ROCm 6.4.3 with a CTranslate2 v3.23.0 source build (`-DCMAKE_HIP_ARCHITECTURES=gfx1151 -DGPU_TARGETS=gfx1151 -DGPU_RUNTIME=HIP -DWITH_CUDA=ON -DWITH_CUDNN=ON`). Reported 138× realtime tiny and ~9× realtime large-v3 vs ~4.45× for whisper.cpp HIP on the same box. Pre-dates upstream ROCm support; superseded by the v4.7.1 wheel.

### 2. ROCm on gfx1151 — current state

- **gfx1151 is NOT in AMD's official ROCm support matrix** as of ROCm 7.2.3 (latest stable). It works in practice on ROCm 6.4.3, 7.0.x, 7.1, 7.2.x, but Strix Halo support is community-driven. Reference: ROCm/ROCm issue #6034 ("Strix Halo gfx1151: 93 ML experiments, 5 critical bf16 bugs, AOTriton 19x speedup undocumented", opened by GitHub user `bkpaine1` on Mar 13 2026).
- **Recommended ROCm version: 7.2.2 (current stable) or 7.2.3.** rocBLAS Tensile kernels for gfx1151 are shipped — verify with `ls /opt/rocm/lib/rocblas/library/ | grep gfx1151` (expect `Kernels.so-000-gfx1151.hsaco`). ROCm 7.0.2 → 7.2 upgrade procedure is documented in production (tinycomputers.io, Apr 2026, Bosgame mini-PC with Ryzen AI Max+ 395, 32 GB DDR5 / 96 GB allocatable to GPU).
- **`HSA_OVERRIDE_GFX_VERSION` is no longer needed on ROCm 7.2.2+** with native gfx1151 kernels. nabe2030 explicitly recommends NOT setting it. If you must override on older ROCm (7.0/7.1 or some TheRock builds), the correct value is `HSA_OVERRIDE_GFX_VERSION=11.5.1` (true gfx1151 ISA); `11.0.0` spoofs as gfx1100 and is a legacy workaround.
- **Bleeding edge: TheRock nightlies (ROCm 7.11 / 8.0 preview).** Significantly faster than stable 7.2 for some workloads per kyuz0 testing on Framework Desktop, but with a 64 GB allocation cap bug as of Dec 2025 (ROCm/TheRock #4645) and ongoing bf16 regressions. For Whisper workloads (3–10 GB VRAM use), the cap is irrelevant; stay on 7.2.x stable for predictability.
- **Kernel: 6.18.4+ on the host.** Kernels older than 6.18.4 have a known gfx1151 stability bug (kyuz0/amd-strix-halo-toolboxes README). Avoid `linux-firmware-20251125`, which breaks ROCm on Strix Halo. Recommended host cmdline: `amd_iommu=off amdgpu.gttsize=126976 ttm.pages_limit=32505856` for max GTT (5–12% measured improvement vs `iommu=pt` on llama.cpp; likely small/zero benefit for Whisper specifically).

### 3. PyTorch ROCm wheels for gfx1151

- **PyPI `torch+rocm6.2` is too old** — using it on a ROCm 7.x system produces `cuBLAS failed` / `No HIP GPUs available`.
- **Use AMD's official manylinux wheels for ROCm 7.2.x:** `https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.1/` provides `torch-2.9.1+rocm7.2.1.lw.gitff65f5bc-cp312-cp312-linux_x86_64.whl`, `torchaudio-2.9.0+rocm7.2.1.gite3c6ee2b-cp312-cp312-linux_x86_64.whl`, `triton-3.5.1+rocm7.2.1.gita272dfa8-cp312-cp312-linux_x86_64.whl`. Or use the `rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0` Docker image (last pushed within 24 h of May 20 2026 per Docker Hub).
- **For nightlies on gfx1151:** `pip install torch --index-url https://rocm.nightlies.amd.com/v2/gfx1151/` (TheRock). Wheels here are native gfx1151 with AOTriton baked in; **no HSA override needed**. PyTorch 2.11.0a0+rocm7.11.0a20260106 documented working on gfx1151 in ROCm/ROCm #6034.
- **wav2vec2 alignment + pyannote** run on the iGPU as standard `torch.cuda` ops (CUDA API is emulated by ROCm). First-run flash-attention JIT under AOTriton takes 1–2 min; cached to `~/.triton/cache/`. `torchcodec` lacks ROCm wheels — pyannote falls back to torchaudio (safe to ignore the warning, or `pip uninstall torchcodec`).
- **Known PyTorch bug:** `PYTORCH_HIP_ALLOC_CONF=backend:malloc` (set in some ROCm shell profiles) crashes PyTorch (ROCm/ROCm #6034). Unset it before launching the container.

### 4. Benchmarks on Strix Halo / Radeon 8060S (gfx1151)

| Stack | Model | Audio | Throughput | Source |
|---|---|---|---|---|
| CTranslate2 v4.7.1 ROCm wheel + faster-whisper, fp16 | large-v3 | 11 s English (JFK) | **~11.5× rt** (10-run avg) | nabe2030/faster-whisper-rocm-strix-halo |
| Same | large-v3 | 30 min 44 s Japanese | **6.56× rt** (4 min 41 s wall), 573 segments, 0 GPU faults, peak 35 GB VRAM | nabe2030 |
| CTranslate2 v3.23.0 source (gfx1151) + faster-whisper | large-v3 | 11 s JFK | ~9× rt | davidguttman/whisper-rocm |
| Same | tiny | 11 s JFK | 138× rt | davidguttman |
| whisper.cpp 1.8.0 + GGML_HIP=ON (ROCm 7.0.1) | large-v3 | 11 s JFK | ~4.45× rt | davidguttman + ggml-org/whisper.cpp #3459 |
| whisper.cpp + Vulkan (RADV) | large-v3-turbo | 1 h 20 m | ~3 min wall | ggml-org/whisper.cpp #3460 |

- **VRAM footprint:** large-v3 fp16 ≈ 3 GB model + ~5–6 GB activations on long-form → ~9 GB working set; small relative to the 96 GB UMA cap. Pyannote adds ~1–2 GB.
- **Concurrent streams:** not publicly benchmarked. With 96 GB UMA you can co-locate large-v3 + an LLM, but expect contention on Strix Halo's 256 GB/s LPDDR5X-8000 bus.
- **Diarization throughput on the 8060S iGPU:** not separately benchmarked publicly. pyannote/speaker-diarization-community-1 runs at several-x realtime on gfx1100 (RX 7900 XTX) per WhisperX Discussion #1364; expect similar or modestly slower on the 8060S.

### 5. Alternative backends and OpenAI-compatible servers

- **whisper.cpp** has working HIP, ROCm, and Vulkan backends on gfx1151. CTranslate2-rocm is ~2× faster than whisper.cpp HIP per davidguttman. **No integrated alignment/diarization** — you lose the WhisperX value-add. On Strix Halo, Vulkan is more stable than HIP for llama.cpp's LLM path; for whisper.cpp HIP is fine.
- **OpenAI-compatible HTTP servers wrapping faster-whisper:**
  - `speaches-ai/speaches` (formerly fedirz/faster-whisper-server) — exposes OpenAI `/v1/audio/transcriptions` and `/v1/audio/translations`, SSE streaming, WebSocket realtime, dynamic model loading. **No upstream ROCm image** — only `ghcr.io/speaches-ai/speaches:latest-cuda` and `:latest-cpu`. Trivial fork: swap base, install CT2-rocm wheel.
  - `pigeekcom/wyoming-faster-whisper-rocm:rocm7.0-strix` — Wyoming protocol (port 10300), not OpenAI-compatible, turnkey for Home Assistant.
  - `jjajjara/rocm-whisper-api` — simpler ROCm-based whisper HTTP API; ROCm 6.3.4 base, older PyTorch. Not WhisperX.
  - `ghecko/whisperx-rocm-docker` — closest match to the user's stated stack; CLI-style (`docker compose run --rm whisperx …`), not a long-running OpenAI server. Wrap with a thin FastAPI shim for HTTP.
  - `OpenNMT/ctranslate2-web-server` — mentioned in CT2 README as an OpenAI-compatible REST API on top of CT2. Works wherever the ROCm wheel works. No diarization.
- **vLLM on gfx1151:** broken as of v0.17.0 — vllm-project/vllm issue #36615 ("[Bug]: unknown error trying to run vllm v0.17.0 with ROCm on Radeon 8060S (gfx1151)", opened Mar 10 2026 by GitHub user `anomaly256`, who reports the same failure on prebuilt v0.15.1, v0.16.0, and v0.17.0 vllm-rocm Docker images plus a locally built one). aiter also breaks with `KeyError: 'gfx1151'` (ROCm/aiter #1415). **Don't use vLLM for Whisper on Strix Halo.**

### 6. WhisperX-on-AMD reference stacks

- **m-bain/whisperX Discussion #1364 — "WhisperX on AMD GPU (ROCm 7.2) — Ubuntu Installation Guide"** (started by `muscleriot` on Mar 7 2026; m-bain/whisperX has 20.8k stars / 2.2k forks as of May 2026) — tested on RX 7900 XTX (gfx1100), should generalize. Working stack: ROCm 7.2 + PyTorch 2.8.0 (`+rocm7.2.0.lw.gitbf943426`, AMD-built) + CTranslate2-rocm 4.1.0 (source-built) + faster-whisper 1.2.1 + WhisperX 3.7.4 + pyannote.audio 3.4.0. CT2 build flags: `-DCMAKE_HIP_ARCHITECTURES=gfx1100 -DCMAKE_CXX_COMPILER=amdclang++ -DWITH_HIP=ON -DWITH_CUDNN=ON` (replace gfx1100 with gfx1151).
- **ghecko/whisperx-rocm-docker** (3 commits, 4 stars, 2026) — Strix Halo/gfx1151 verified. Uses pyannote.audio ≥4.0 (community-1 diarization model), patches WhisperX's `diarize.py` to route HF token via env var, multi-stage builder copies `/opt/ctranslate2` + wheel into the runtime image.
- **BoredYama/whisperX-AMD-ROCM7.1** — ROCm 7.1/7.2 native (no Docker), conda-based. Documents `--no-deps` install pattern for WhisperX and explicit `pip uninstall torchcodec` workaround.

---

## Details — Container Strategy

### 6.1 Choice of base image (May 2026)

| Stage | Image | Rationale |
|---|---|---|
| Builder (if source-building CT2) | `rocm/dev-ubuntu-24.04:7.2.2-complete` | Full ROCm SDK incl. hipcc, hipblas-dev, hipcub-dev, rocrand-dev, rocthrust-dev, miopen-hip-dev, composable-kernel-dev. No Python venv. |
| Runtime (recommended) | `rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0` | PyTorch 2.10.0 built against ROCm 7.2.2, Python 3.12, ROCm libs in `/opt/rocm`. Bundled `/opt/venv`. ~25 GB. Same base ghecko uses; PyTorch 2.10.0 has the ROCm 7.2.x bindings pyannote.audio 4.0+ requires. |
| Slimmer alternative | `rocm/dev-ubuntu-24.04:7.2.2` + manual `pip install torch==2.9.1+rocm7.2.1` from `https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.1/` | If you don't need the bundled PyTorch. |

### 6.2 Containerfile (multi-stage, gfx1151-targeted)

Two variants. **Variant A** uses the official v4.7.1 wheel (no source build); **Variant B** source-builds CT2 (ghecko-style) when you need a flag the wheel lacks.

**Variant A — official v4.7.1 wheel (recommended, ~10 min image build):**

```dockerfile
# syntax=docker/dockerfile:1.7
# podman build -t whisperx-rocm:v4.7.1-gfx1151 -f Containerfile .
# Final size: ~28-30 GB (PyTorch + ROCm libs dominate)
FROM rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0

ENV DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/cache/huggingface \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Do NOT set HSA_OVERRIDE_GFX_VERSION on ROCm 7.2.2 — native gfx1151
    AMDGPU_TARGETS=gfx1151 \
    LD_LIBRARY_PATH=/opt/rocm/lib:/opt/rocm/lib/llvm/lib:${LD_LIBRARY_PATH}

# 1. System deps for audio decoding and downloads
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates unzip git libnuma1 libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Pin transitive deps to whatever the rocm/pytorch image already has,
#    so pip never silently swaps ROCm torch for PyPI CUDA torch.
RUN /opt/venv/bin/python - <<'PY' > /opt/constraints.txt
import torch, torchvision, torchaudio
print(f"torch=={torch.__version__.split('+')[0]}")
print(f"torchvision=={torchvision.__version__.split('+')[0]}")
print(f"torchaudio=={torchaudio.__version__.split('+')[0]}")
PY

# 3. CTranslate2 v4.7.1 ROCm wheel (gfx1151 kernels baked in).
RUN curl -fL -o /tmp/ct2-rocm.zip \
      https://github.com/OpenNMT/CTranslate2/releases/download/v4.7.1/rocm-python-wheels-Linux.zip \
 && unzip -j /tmp/ct2-rocm.zip 'temp-linux/ctranslate2-4.7.1-cp312-*manylinux*x86_64.whl' -d /tmp/ \
 && /opt/venv/bin/pip install --no-cache-dir -c /opt/constraints.txt /tmp/ctranslate2-4.7.1-cp312-*.whl \
 && rm -rf /tmp/ct2-rocm.zip /tmp/ctranslate2-*.whl

# 4. WhisperX + faster-whisper + pyannote (HF gated, token only at runtime)
RUN /opt/venv/bin/pip install --no-cache-dir -c /opt/constraints.txt \
        "numpy>=2.1.0" "transformers>=4.48.0" accelerate nltk omegaconf hf-transfer \
        "faster-whisper==1.2.1" \
        "pyannote.audio>=4.0.0" \
 && /opt/venv/bin/pip install --no-cache-dir -c /opt/constraints.txt --no-deps \
        "whisperx==3.7.4"

# 5. Pyannote 4.x compatibility shim — WhisperX still calls use_auth_token in places
RUN find /opt/venv/lib/python3.12/site-packages/pyannote -type f -name "*.py" \
        -exec sed -i 's/use_auth_token/token/g' {} +

# 6. torchcodec lacks ROCm wheels; remove to silence pyannote warning
RUN /opt/venv/bin/pip uninstall -y torchcodec || true

# 7. Non-root user; UID 1000 matches typical host. --userns=keep-id maps it directly under rootless.
RUN useradd -m -u 1000 -s /bin/bash whisperx \
 && mkdir -p /cache/huggingface /work \
 && chown -R whisperx:whisperx /cache /work
USER whisperx
WORKDIR /work

# 8. HF_TOKEN injected at runtime via --secret. Never bake into the image.
# 9. Default to WhisperX CLI; in production override with a FastAPI shim (see §6.3).
ENTRYPOINT ["/opt/venv/bin/python", "-m", "whisperx"]
```

**Variant B — source-built CTranslate2 (use only if you need e.g. WITH_FLASH_ATTN).** Canonical reference: `https://github.com/ghecko/whisperx-rocm-docker/blob/main/Dockerfile.rocm`. Multi-stage with `FROM rocm/dev-ubuntu-24.04:7.2 AS builder` + oneDNN 3.11 static + CTranslate2 master `-DCMAKE_HIP_ARCHITECTURES="gfx1151" -DCMAKE_CXX_COMPILER=/opt/rocm/bin/hipcc -DWITH_HIP=ON -DWITH_DNNL=ON -DWITH_MKL=OFF`. Build time 20–40 min. The builder stage carries the full ROCm SDK (~25 GB) but only `/opt/ctranslate2/lib/*.so` and the Python wheel are `COPY --from=builder` into the runtime image, keeping the final size at ~28–30 GB.

### 6.3 OpenAI-compatible HTTP endpoint

No upstream OpenAI-compatible Whisper server natively supports ROCm. Two pragmatic options:

1. **Fork `speaches`** (`ghcr.io/speaches-ai/speaches:latest-cpu`) — swap the base to your `whisperx-rocm:v4.7.1-gfx1151`, point its faster-whisper executor at `/cache/huggingface`. Gets you `/v1/audio/transcriptions` and `/v1/audio/translations` with SSE streaming and WebSocket realtime in ~50 lines of glue. No diarization or word timestamps.
2. **Wrap `ghecko/whisperx-rocm-docker` behind a ~30-line FastAPI** that calls `whisperx.transcribe()` + `whisperx.align()` + `whisperx.DiarizationPipeline()`. Gives you a fully self-hosted OpenAI-compatible endpoint with word-level timestamps and speaker labels — features off-the-shelf Whisper servers don't expose. **Recommended for the user's pipeline.**

For pure transcription, `OpenNMT/ctranslate2-web-server` is the simplest path — OpenAI-compatible REST on top of the v4.7.1 ROCm wheel.

### 6.4 Podman/Docker invocation

**Rootful Podman (simplest, most reliable):**
```bash
podman run -d --name whisperx \
  --device /dev/kfd --device /dev/dri \
  --group-add video --group-add render \
  --ipc=host --shm-size 8g \
  --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
  -p 9000:9000 \
  -v whisperx-hf-cache:/cache/huggingface \
  -v /srv/audio:/work:Z \
  --secret hf-token,type=env,target=HF_TOKEN \
  whisperx-rocm:v4.7.1-gfx1151 \
    --model large-v3 --compute_type float16 --device cuda --diarize \
    --output_dir /work/out --output_format json
```

**Rootless Podman (preferred per user constraints):**
```bash
# Host prereqs (once)
sudo usermod -aG video,render $USER && newgrp render
# udev for KFD (Ubuntu; Fedora/Arch typically OK out of the box):
sudo tee /etc/udev/rules.d/70-amdgpu.rules <<'EOF'
SUBSYSTEM=="kfd",  KERNEL=="kfd",         MODE="0660", GROUP="render"
SUBSYSTEM=="drm",  KERNEL=="renderD*",    MODE="0660", GROUP="render"
EOF
sudo udevadm control --reload && sudo udevadm trigger

podman run -d --name whisperx \
  --userns=keep-id \
  --device /dev/kfd --device /dev/dri \
  --group-add keep-groups \
  --security-opt seccomp=unconfined \
  --security-opt label=disable \
  --ipc=host --shm-size 8g \
  -p 9000:9000 \
  -v whisperx-hf-cache:/cache/huggingface \
  -v /srv/audio:/work:Z \
  --secret hf-token,type=env,target=HF_TOKEN \
  whisperx-rocm:v4.7.1-gfx1151
```

Key rootless flags:
- **`--group-add keep-groups`** — Podman 3.2+ feature using `crun` to skip `setgroups()` so the in-container process inherits the host user's supplementary `render`/`video` group access to `/dev/kfd`. `--group-add render` alone is broken under user namespaces because the in-container `render` GID is offset (host GID 39 → container GID 100038), which the kernel ACL does not honor. Background: redhat.com/en/blog/files-devices-podman.
- **`--security-opt label=disable`** — required on SELinux distros (Fedora/RHEL) to access `/dev/dri/renderD128` from a rootless container (containers/podman #18497). Without it, `vainfo`/`rocminfo` fails with EACCES (`amdgpu_bo_cpu_map failed -13`).
- **`--security-opt seccomp=unconfined`** — ROCm uses `AMDKFD_IOC_*` ioctls that Podman's default seccomp profile blocks on some distros. Without it, `hsa_init` returns `HSA_STATUS_ERROR_OUT_OF_RESOURCES` (ROCm/ROCm #3144).
- **`--userns=keep-id`** — maps host UID 1000 → container UID 1000 so the model cache volume stays writable.
- **`--ipc=host --shm-size 8g`** — PyTorch DataLoader / pyannote shared memory.
- **Both `/dev/kfd` and `/dev/dri`** are required for ROCm. ROCm/ROCm #3144 documents that restricting to a single `renderD12X` in a rootless container breaks `hsa_init` on multi-GPU systems — pass the whole `/dev/dri` or run rootful. On a single-iGPU Strix Halo box this is moot.

**Ansible note (per user's stack):** wrap the above as a Podman Quadlet under `/etc/containers/systemd/whisperx.container` with `[Container]` directives `AddDevice=/dev/kfd`, `AddDevice=/dev/dri`, `GroupAdd=keep-groups`, `SecurityLabelDisable=true`, `Secret=hf-token,type=env,target=HF_TOKEN`. Quadlets generate native systemd units, integrate cleanly with Ansible's `systemd_service` module, and survive reboots. For Squid egress, set `Environment=HTTPS_PROXY=http://squid:3128 HF_HUB_ETAG_TIMEOUT=30` so HF model pulls go through the proxy.

### 6.5 Image size and slimming

- `rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0` is ~25 GB uncompressed (bundles PyTorch + a full ROCm runtime). The official v4.7.1 wheel adds ~150 MB. WhisperX + pyannote + transformers add ~3 GB. **Final Variant A image: ~28–30 GB.**
- Source-built Variant B without multi-stage easily reaches **60+ GB**. ghecko's two-stage pattern (`rocm/dev-ubuntu-24.04:7.2 AS builder` → runtime, COPYing only `/opt/ctranslate2` and the wheel) brings it back to ~30 GB.
- To slim further, swap the runtime to `rocm/dev-ubuntu-24.04:7.2.2` and install only `rocm-hip-runtime` + `rocblas` + `hipblas` + `hipblaslt` + `miopen-hip` instead of the full `rocm` meta-package — saves ~10 GB, but loses dev headers for in-container debugging.
- The HF model cache (whisper-large-v3 = 3 GB + pyannote/speaker-diarization-community-1 ≈ 200 MB + wav2vec2 alignment models ≈ 1 GB per language) should be a **named volume**, NOT baked into the image. Build-time bake (`--build-arg HF_TOKEN=…`) is supported in ghecko's Dockerfile for air-gapped use but inflates by ~5 GB and pins the model version into the image.

### 6.6 HF_TOKEN handling

Pyannote diarization models (`pyannote/speaker-diarization-3.1`, `pyannote/speaker-diarization-community-1`) are **HF gated** — you must accept the EULA on huggingface.co with your account, then pass a fine-grained read-only token at runtime. Never bake the token into the image.

**Podman secret (recommended):**
```bash
printf 'hf_xxxxxxxxxxxxxxxxxxxxxxxxxx' | podman secret create hf-token -
podman run ... --secret hf-token,type=env,target=HF_TOKEN ...
```
**Ansible:** keep the token in `ansible-vault`, render to `/etc/whisperx/hf-token` (mode 0600, owner root), and create the Podman secret at container startup via a systemd one-shot:
```ini
[Unit]
Before=whisperx.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'podman secret rm hf-token 2>/dev/null; cat /etc/whisperx/hf-token | podman secret create hf-token -'
RemainAfterExit=yes
```

### 6.7 BIOS / UEFI and host kernel caveats

- **UMA Frame Buffer Size in BIOS:** default is typically 64 GB. For Whisper alone this is plenty. To co-locate LLMs, raise to 96 GB on boards that expose the setting (some Bosgame/GMKtec) or rely on dynamic GTT. On HP ZBook Ultra G1a and similar boards exposing only Auto/Game/AI modes, runtime can allocate up to ~110 GB via GTT regardless. `rocminfo` should report ~110 GB GLOBAL pool.
- **Host kernel:** 6.18.4+ is required for stable gfx1151. Ubuntu 26.04 inbox (7.0.0-14-generic) is verified by nabe2030. Ubuntu 24.04 LTS requires HWE.
- **Host cmdline:** `amd_iommu=off amdgpu.gttsize=126976 ttm.pages_limit=32505856`. Avoid `linux-firmware-20251125` and any kernel ≤6.18.3.
- **amdgpu DKMS:** AMD's noble apt repo no longer ships `amdgpu-dkms` for Ubuntu 26.04; use the inbox driver. On Ubuntu 24.04 you can still install DKMS but it must match the kernel exactly. Inbox is recommended for predictability.

### 6.8 Verified-vs-experimental matrix

| Claim | Status | Primary source |
|---|---|---|
| CT2 v4.7.1 official ROCm wheel runs on gfx1151 without source build, no HSA override | **Verified** (Apr/May 2026, GMKtec EVO-X2) | github.com/nabe2030/faster-whisper-rocm-strix-halo |
| WhisperX + pyannote 4.x + diarization in a container on Strix Halo | **Verified** (Mar 2026; CT2 source-built, not wheel) | github.com/ghecko/whisperx-rocm-docker |
| `rocm/pytorch:rocm7.2.2_ubuntu24.04_py3.12_pytorch_release_2.10.0` as base | **Verified** runtime image; daily Docker Hub push | hub.docker.com/r/rocm/pytorch/tags |
| Rootless Podman with `--group-add keep-groups` + ROCm | **Verified** for inference; documented multi-GPU edge case | oneuptime.com/blog (Mar 2026), ROCm/ROCm #3144, redhat.com files-devices-podman |
| Variant A image in this report (wheel-based, no source build) | **Synthesized** — every component is verified individually but not yet published as a single image | — |
| Long-form audio stability on gfx1151 | **Verified at 30 min**; **unverified at 1 h+** | nabe2030 |
| Pyannote diarization throughput on 8060S | **Unverified** on gfx1151 (benchmarked on gfx1100 only) | m-bain/whisperX #1364 |
| vLLM for Whisper serving on gfx1151 | **Broken** | vllm-project/vllm #36615 |
| `pigeek/CTranslate2-rocm` GitHub repo | **Unverifiable** at time of writing (Docker image exists; GitHub repo could not be fetched) | hub.docker.com/r/pigeekcom/wyoming-faster-whisper-rocm |

---

## Recommendations

**Stage 1 — Get something working in a day (lowest risk):**
1. Clone `ghecko/whisperx-rocm-docker` and `docker compose build whisperx` on a Strix Halo host running kernel ≥6.18.4. No host ROCm install required — the container ships its own.
2. Validate with `samples/jfk.wav` and a 5-minute sample of your own audio; confirm `--device cuda` (ROCm under the hood) returns word-level timestamps and speaker labels.
3. Wrap in a ~30-line FastAPI shim to expose `/v1/audio/transcriptions` (returning `verbose_json` with word timestamps and speaker labels).
4. Deploy via Podman Quadlet + Ansible, with HF token from `ansible-vault`.

**Stage 2 — Modernize to the v4.7.1 wheel (Variant A above):**
1. Build the Containerfile in §6.2 with `podman build`. ~10 min vs ~40 min for source-built.
2. A/B benchmark vs Stage 1 on a 30-min sample. Expect parity or modest (~5–10%) improvement from AMD's official Tensile kernels plus the wheel's coverage of `gfx950/gfx1201` (RDNA4) in addition to gfx1151.
3. If stable for 1 week, promote to production. Pin `whisperx-rocm:v4.7.1-gfx1151-2026-05-XX` immutably in your Ansible inventory.

**Stage 3 — Optional optimizations:**
1. Switch to TheRock nightly PyTorch wheels for AOTriton flash-attention in the alignment phase (measure first; the 19× speedup figure from ROCm/ROCm #6034 is bkpaine1's training/attention benchmark, not necessarily inference). Trade-off: nightlies have known bf16 regressions and a 64 GB allocation cap bug as of Dec 2025.
2. Try `distil-large-v3` (turbo) for ~2× transcription speed at slightly higher WER on non-English; same CT2 path.
3. Co-locate an LLM via llama.cpp ROCm with 96 GB UMA, but expect LPDDR5X bandwidth contention.

**Thresholds that should change the plan:**
- **CT2 v4.8.x with further gfx1151 tuning lands:** rebase wheel URL.
- **ROCm 8.0 GA puts gfx1151 in the official support matrix:** drop `--security-opt seccomp=unconfined` if AMD relaxes KFD ioctls into Podman's default profile (don't bet on it).
- **>1 h audio reproduces `Memory access fault by GPU node-1`:** fall back to application-layer chunking (10–15 min windows) or test paralin/ctranslate2-rocm's mitigations.
- **pyannote 5.x bumps PyTorch requirement to ≥2.11:** rebase to `rocm/pytorch:rocm7.2.3_ubuntu24.04_py3.12_pytorch_release_2.11.x` once published.
- **Speaches/faster-whisper-server upstream merges a ROCm path:** drop the FastAPI shim, use the official server.

---

## Caveats

- **gfx1151 is unsupported by AMD officially.** Every working configuration described here is community-validated. ROCm point releases (e.g., 7.1 → 7.2) have broken gfx1151 in the past and may again. Pin ROCm version in the container; pin host kernel; test before upgrading.
- **The exact gfx-target list embedded in CTranslate2 v4.7.1's ROCm .so is not documented in OpenNMT's release notes** — it was discovered by `strings` inspection by nabe2030. Treat gfx1151 inclusion as effective-but-undocumented; verify with `python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count()); print(ctranslate2.get_supported_compute_types('cuda'))"` immediately after install.
- **`pigeek/CTranslate2-rocm` and `pigeek/wyoming-faster-whisper-rocm` GitHub repos could not be independently verified** at time of writing — only the Docker image `pigeekcom/wyoming-faster-whisper-rocm` (last push ~Oct 2025) is verifiable. Use only as last-resort fallback.
- **WhisperX 3.7.4 + pyannote.audio 4.0+ requires `--no-deps` install plus source patches** (the `use_auth_token → token` rename and `Pipeline.from_pretrained` signature change). ghecko's sed-based patches are brittle and will likely break on a future WhisperX point release; pin `whisperx==3.7.4` strictly.
- **`HSA_OVERRIDE_GFX_VERSION` recommendations vary by guide.** On ROCm 7.2.2+ with a native gfx1151 kernel, leave it unset. On ROCm 7.0/7.1 or TheRock nightlies you may need `HSA_OVERRIDE_GFX_VERSION=11.5.1` (true gfx1151 ISA) or `11.0.0` (spoof as gfx1100). ghecko's image sets `11.5.1` defensively — harmless on 7.2.2, redundant.
- **Image sizes will surprise you.** A "minimal" ROCm 7.2 + PyTorch + WhisperX + pyannote container is ~28 GB. Plan registry storage and pull bandwidth (notably across a Squid proxy) accordingly.
- **Benchmarks above are single-stream.** Concurrent multi-stream throughput on the 8060S iGPU is not publicly benchmarked for Whisper. The 256 GB/s LPDDR5X-8000 bus will be the bottleneck before compute on diarization-heavy workloads.
- **Diarization speed has not been benchmarked on gfx1151 specifically.** Throughput numbers above cover transcription only.
- **CHANGELOG silence.** OpenNMT/CTranslate2's README states only: *"If you have an AMD ROCm GPU, we provide specific Python wheels on the releases page."* gfx1151 and Strix Halo appear nowhere in OpenNMT's own documentation. This is normal for an officially-unsupported target but means the user must verify each ROCm wheel release independently.
- **Information half-life on Strix Halo is short.** This report reflects state as of May 20, 2026. ROCm 7.3/8.0 and CTranslate2 4.8 are likely within months and may invalidate specific version pins. Re-verify the v4.7.1 wheel URL, the `rocm/pytorch` tag, and the ghecko sed expressions before deploying to a new host.
