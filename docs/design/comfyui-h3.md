# ComfyUI + MiniMax H3 on Nvidia — Container Strategy, Aug 2026

## TL;DR

- Run the **full ComfyUI backend** in one podman container on a single host
  GPU (NVIDIA CDI), serving the MiniMax H3 omni-modal video model. ComfyUI is
  a single-process server: all graph execution, conditioning, sampling, and
  VAE decode happen inside the container; users only need a browser/API
  client. There is no supported way to run "just H3" remotely and run the
  rest of the graph on the client.
- **Model weights are referenced from a host directory, never baked into the
  image and never downloaded at runtime.** ComfyUI is folder-based: with the
  safetensors pre-placed it does zero outbound fetches in steady state. We
  bind-mount the model tree (local) and the user-data tree (NAS) into the
  container.
- **User data (outputs, uploads, per-user workflows) lives on the NAS at
  `/near1`, not on the box.** It's small and user-visible, so it gets
  durability on the existing `/near1` NAS share and survives container/host
  rebuilds and GPU-host replacement. **Static model weights stay on local host
  NVMe** for fast repeated reads.
- The container sees stable mount paths; the host dirs backing each mount can
  move later (e.g. models to a NAS for multi-host dedup) via a host-side
  remount, never an image change.
- One container image built once, pushed to `registry.xc:5000/comfyui:VERSION`,
  pulled by future Nvidia hosts. Systemd unit + one rollback-unit like the
  MinerU podman refactor.
- This is a **device-bound, non-OpenAI service**: exposed via the frontend /
  frp reverse proxy (HTTP + WebSocket), *not* routed through the
  `local-ai-proxy` OpenAI inference gateway.

## Context

MiniMax H3 (`MiniMaxAI/MiniMax-H3`, released Aug 2–3 2026) is a general-purpose
omni-modal video model: it jointly understands text/image/video/audio and
generates 768p video with **native 32 kHz stereo audio** in a single diffusion
pass (24 fps, 4–15 s). It is a ~33B-param DiT conditioned on a Qwen3-VL-32B
text encoder. Two weight sets:

- **FL2VA** — first/last-frame control → T2V / I2V / first+last-frame-to-video.
- **Ref2VA** — reference-driven generation (≤9 images, ≤3 videos, ≤3 audio),
  for character/style/motion/voice-locking.

Open-weights output is 768p native (768 px short edge, capped 768×1344). The
2K pass (`H3-Regenerate-2K`) and the `H3-Context-IR` input-reasoning stage are
hosted-API only and not reproducible from the released weights.

ComfyUI has native H3 support since 0.30.0 (nodes in
`comfy_extras/nodes_minimax_h3.py`, model in `comfy/ldm/minimax/model.py`) and
repackages the weights at `Comfy-Org/MiniMax-H3` (HF) in quantized, ComfyUI-loadable
single-file formats.

## Decisions

### 1. One whole GPU, one container (podman + CDI)

ComfyUI + H3 wants a complete GPU and doesn't co-tenant well (768p needs
~24 GB VRAM; smaller cards force 480p / `--lowvram` offload). Pin a single
host-visible GPU to the container via NVIDIA CDI:

```
--device nvidia.com/gpu={{ comfyui_gpu }}        # CDI device index
NVIDIA_VISIBLE_DEVICES={{ comfyui_gpu }}         # belt + suspenders
```

This mirrors the MinerU podman refactor (podman + NVIDIA Container Toolkit +
CDI on the Nvidia hosts, `--userns=keep-id` running as `dev` UID 1002).
Play is gated on a per-host `comfyui_enabled` opt-in and ends early when
`nvidia_host_gpu_count` is 0 (same guard as MinerU). A GPU-passthrough Incus
VM (nv2/nv3-style hosts) is the heavier isolation alternative if a workload later
needs it; the container is the lighter, precedent-consistent default.

### 2. Weights referenced from a host dir (not baked, not fetched at runtime)

ComfyUI loads models from `<comfyui>/models/{diffusion_models,text_encoders,vae}`
and errors if a file is absent — it does not auto-fetch. The frontend Template
Library / ComfyUI-Manager can download weights interactively, but we don't
enable that path. Instead:

- Host model root: `/home/dev/comfyui/models/` (in the `dev` user's home).
- Container model path: `/opt/comfyui/models` (fixed, stable).
- Bind-mount, `:ro` once seeded.

Files are populated **once** (pinned, from `Comfy-Org/MiniMax-H3`) into the
host dir by an admin/seed step — this is asset staging, not a per-deploy
runtime fetch, and it keeps the image small and the container egress-free.

### 3. Image carries the app only

```
-v /home/dev/comfyui/models:/opt/comfyui/models:ro     # local NVMe, seeded
-v /near1/comfyui/output:/opt/comfyui/output           # NAS, user data
-v /near1/comfyui/input:/opt/comfyui/input             # NAS, uploads (I2V/R2V)
-v /near1/comfyui/user:/opt/comfyui/user               # NAS, workflows/settings
-v /home/dev/.cache/huggingface:/home/dev/.cache/huggingface
```

Image (~12 GB) = Ubuntu 24.04 base + Python 3.12 venv (uv) + ComfyUI (pinned
git SHA that includes the H3 nodes) + `torch`/`torchvision`/`torchaudio` CUDA
wheels + `ffmpeg` for MP4 muxing. Weights/caches stay out of the image, so the
image survives `podman rm` and rebuilds without re-staging ~40 GB of models.

### 4. User data on the NAS (`/near1/comfyui`)

ComfyUI's only genuinely user-visible state is small: generated output
(`output/`), uploaded inputs for I2V/Ref2VA (`input/`), and per-user workflow
files (`user/`). That's what we persist — it goes on the **existing `/near1`
NAS** (Samba/CIFS, already auto-mounted on `compute` nodes; `nvidia` ⊂
`compute`, so the GPU hosts see it) under
`/near1/comfyui/{output,input,user}`. Durable, shared, survives `podman rm`,
rebuilds, and GPU-host replacement. **No new share** — the existing `/near1`
mount covers it.

The ~59 GB of model weights are **not** user data: they're static,
re-seedable, and read on every load, so they stay on fast local host NVMe
(`/home/dev/comfyui/models`), not on CIFS. If multi-host model dedup ever
becomes worth it we can revisit, but serving reads over SMB — and making the
NAS a serving SPOF — is not the default.

**Write permission (the one real constraint).** `/near1` is mounted with
`uid=1000,gid=1000,file_mode=0664,dir_mode=0775`; our container runs as `dev`
(UID 1002) via `--userns=keep-id`, which lands as "other" on the mount → no
write. But the mount presents every directory as gid 1000 / 0775, so the fix
is to give the container process **supplemental group 1000** — no second share:

- Run `podman ... --group-add 1000` alongside `--userns=keep-id`. `dev` stays
  UID 1002; the extra gid 1000 puts the process in the group-write class on
  the mount, so it can create/read its own files under `/near1/comfyui/`.
- Pre-create `/near1/comfyui/{output,input,user}` (owned by group 1000, i.e.
  just `0775`) in the play before the unit starts.

No Samba/SMB changes and no change to the shared `near1` mount or its uid.
Group propagation through `--userns=keep-id` is verified at deploy (see TODO);
a fallback would be running the container as root, but that breaks the
keep-id `/home/dev` bind-mount pattern, so `--group-add` is the first choice.

### 5. No outbound egress in steady state

The container is bind-only for models and does not need HF/ModelScope at
runtime (no interactive downloads; custom nodes are pre-built into the image).
This lets us run the unit without general outbound access and keeps an
accidental custom-node/model download from reaching the network — aligning
with the repo's "no ad-hoc fetching" policy.

### 6. Quantization selection for NVIDIA

We run the NVIDIA-friendly file set, not the AMD one:

- Diffusion (FL2VA + Ref2VA): `pruned_int8_convrot` (19.5 GB each) — the
  default the official T2V/I2V/R2V templates reference.
- Text encoder: `qwen3vl_32b_minimax_h3_nvfp4_awq` (14.6 GB) — the NVFP4 AWQ
  encoder runs fine on NVIDIA (unlike on ROCm).
- VAEs: `minimax_h3_video_vae_fp16` (4.9 GB) + `minimax_h3_audio_vae_fp32`
  (0.6 GB).

Sage Attention (≈2× sampling speedup) is *deferred*: it ships CUDA extension
wheels that conflict with the "no prebuilt binaries / build playbook" policy,
and its global `--use-sage-attention` path is buggy for H3. If we want it,
add a `build-sage-attention.yml` source build later. Stock attention works
unchanged.

## Model assets on host

| File (under `/home/dev/comfyui/models/`) | Size | Purpose |
|---|---|---|
| `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 19.5 GB | T2V / I2V / first+last-frame |
| `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 19.5 GB | R2V (reference mode) |
| `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14.6 GB | shared text encoder |
| `vae/minimax_h3_video_vae_fp16.safetensors` | 4.9 GB | video decode |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB | audio decode |

Baseline (FL2VA) ≈ **40 GB**; adding Ref2VA ≈ **59 GB** of model files, plus
the ~12 GB image. These are static and sit on local NVMe — user data no longer
counts against this. Size the host home volume with headroom (≥100 GB) for a
future bf16 fallback. A NAS-hosted model tree for multi-host dedup is a later
optimization, not the default (per-host duplication is acceptable with one
Nvidia host today).

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│ Nvidia host (e.g. nv<N>)                                        │
│                                                                 │
│  local NVMe — static model assets:                              │
│    /home/dev/comfyui/models/                                    │
│      ├─ diffusion_models/minimax_h3_{fl2va,ref2va}_pruned_...   │
│      ├─ text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq...       │
│      └─ vae/...                                                 │
│    /home/dev/.cache/huggingface/                                │
│                                                                 │
│  NAS (CIFS, gid=1000): /near1/comfyui                │
│    output/   input/   user/    ← durable user data              │
│                                                                 │
│  comfyui.service (systemd; --group-add 1000)                    │
│    └─ podman run --rm --userns=keep-id                          │
│         ├─ image: registry.xc:5000/comfyui:VERSION              │
│         ├─ --device nvidia.com/gpu={{ gpu }}  (single GPU)      │
│         └─ --group-add 1000  (matches NAS gid)                  │
│              -v /home/dev/comfyui/models:/opt/comfyui/models:ro │
│              -v /near1/comfyui/{output,input,user}:/opt/comfyui/…│
│              -v /home/dev/.cache/huggingface → /home/dev/...    │
│              entrypoint: comfyui --listen 0.0.0.0 --port 8188   │
│                         (no runtime model downloads)            │
│          host TCP:8188 (HTTP + WebSocket /ws)                   │
└───────────────────────┬─────────────────────────────────────────┘
                        │ frontend/frp reverse proxy
                        ▼
             users: browser ComfyUI UI + /prompt API
             (research/edu, thin clients; no GPU needed)
```

## Exposure

- Cluster-internal: `http://<n>.xc:8188` (UI + JSON/WS API).
- External: frontend/frp reverse proxy → the same port; must forward both
  plain HTTP and the WebSocket upgrade path (`/ws`) used by the UI and by the
  workflow-submission channel. Keep it out of the OpenAI `local-ai-proxy`
  gateway (H3 is a diffusion/ComfyUI-protocol service, not OpenAI chat).
- Authentication restraints (if any) belong at the frontend edge, reusing the
  cluster's existing auth for user-facing services.

## Deployment flow (`app/install-comfyui.yml`, mirroring `install-mineru.yml`)

1. End early when `nvidia_host_gpu_count == 0` or `comfyui_enabled` is unset.
2. Ensure podman + NVIDIA CDI present (`nvidia-ctk cdi generate` if missing).
3. Build image on the first host (`buildah bud` from `files/comfyui/Containerfile`,
   ComfyUI pinned to a git SHA), push `registry.xc:5000/comfyui:VERSION`.
4. Pre-create the bind-mount targets: local `/home/dev/comfyui/models` and
   `/near1/comfyui/{output,input,user}` on the NAS (0775, so group 1000 can
   write). `/near1` is already mounted on the `compute`/`nvidia` hosts.
5. Seed model files (one-time, pinned, from `Comfy-Org/MiniMax-H3`) into
   `/home/dev/comfyui/models` — staging, not runtime download.
6. Install `comfyui.service` (single GPU via CDI, bind mounts,
   `--userns=keep-id`, `--group-add 1000` for NAS write access).
7. Start it; smoke test = `GET /system_stats` (200) + submit a minimal T2V
   workflow JSON via `/prompt`, confirm a video + audio file lands on the NAS
   at `/near1/comfyui/output`.
8. Register the reverse-proxy/frontend rule so users reach the UI.

## Rollback

Keep the unit file/version tag scheme simple: rollback = `podman pull`
the previous `registry.xc:5000/comfyui:<prev>` and `systemctl restart`.
Because weights live on the host (not the image), rollback touches only app
code; the model assets and the NAS-resident user data are unchanged.

## Open items / TODO

- [ ] Pick the concrete ComfyUI git SHA that ships the H3 nodes, and the torch
      CUDA wheel set to pin in the Containerfile.
- [ ] Decide the first Nvidia host + which GPU index (`comfyui_gpu`) — a
      host-visible card, not a vfio-pci/passthrough card.
- [ ] Confirm the frontend/frp WebSocket passthrough for `/ws`.
- [ ] Optional: `build-sage-attention.yml` source build for the speedup.
- [ ] Verify `--group-add 1000` + `--userns=keep-id` gives the container group
      write on the `/near1` CIFS mount at deploy (smoke-test a file write before
      wiring the unit).
- [ ] Decide whether multi-host model dedup on `/near1` is ever worth the CIFS
      read path (optional; fine to stay per-host NVMe with one GPU host).
