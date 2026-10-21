# ComfyUI MiniMax H3 (usage)

How to reach the ComfyUI H3 sandbox and load a ready-made text/video
workflow. Deployment and design live in
[`docs/design/comfyui-h3.md`](../design/comfyui-h3.md).

## Access

ComfyUI is exposed as **`https://comfy.<domain>`** — its own nginx vhost on
the storage leader, gated by the cluster IdP (authentik). Sign in with your
cluster account; members of the IdP `comfy` group or `ycluster-admins` get in
(an admin adds you to `comfy` in authentik). The vhost proxies HTTP +
WebSocket (`/ws`) to the backend host, so the UI and workflow submission work
as if local.

Cluster-internal, without the tunnel, the UI+API is `http://<comfyui-host>.xc:8188`
(no auth — reachable only from inside the cluster).

> Bring-up fallback: while the public vhost is still being wired, you can
> tunnel straight to the backend:
>
> ```bash
> ssh -L 8188:<comfyui-host>.xc:8188 <bastion>
> # then open http://localhost:8188
> ```

The service must be active and the model tree fully seeded first. Model
weights (ls under `~/comfyui/models` on the host;
`~/bin/seed-comfyui-models.sh --verify-only` verifies) are bind-mounted into
the container and loaded on demand — no service restart is needed once a file
finishes downloading.

## Loading a pre-built workflow (recommended)

There is no need to wire H3 nodes by hand. Comfy-Org ships official
T2V / I2V / R2V graphs that reference the exact files we seed.

1. Open the ComfyUI UI in your browser.
2. Top menu → **Templates** (the browser pulls the
   `Comfy-Org/workflow_templates` repo).
3. Search **MiniMax** and pick the matching template:
   - **MiniMax H3 T2V** — text-to-video (the quick start)
   - **MiniMax H3 I2V** — image/video to video
   - **MiniMax H3 R2V** — reference-to-video
   - **FastH3 T2V** — the 8-step distilled MiniMax H3 model
4. Fill in your `prompt` (and keyframes/inputs for I2V/R2V) and hit
   **Queue**.

The templates resolve the model files locally from the bind-mounted
`models/` tree, so nothing downloads at runtime.

## FastH3 (8-step distillation)

FastH3 8-Step V2 is a distilled MiniMax H3 checkpoint for faster text-to-video
and audio generation. It shares the base H3 text encoder and VAEs, but uses a
separate 22.1 GB diffusion model. The official T2V template sets the required
`vsa` BlockSparseAttention mode; use that template rather than substituting
FastH3 into a base-H3 workflow.

FastH3 does **not** cover image/first-last-frame conditioning or
reference-to-video. For those, use the base MiniMax H3 I2V/R2V templates.

### Fallback: drag-and-drop the JSON

If the Templates browser is unavailable, drag the raw JSON straight onto
the canvas:

- T2V: `https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_t2v.json`
- I2V: `https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_i2v.json`
- R2V: `https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_minimax_h3_r2v.json`

## Prerequisite: seeding must be complete

T2V needs all of these present before a workflow will execute (the largest,
the text encoder, downloads last):

- `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`
- `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
- `vae/minimax_h3_video_vae_fp16.safetensors`
- `vae/minimax_h3_audio_vae_fp32.safetensors`

If the `Load CLIP` / `Load VAE` dropdowns are empty, seeding isn't done yet.

## Additional model families

The same seeder also stages these image/video-model families. Use the official
Template Library entries for each model:

- **Wan 2.2 5B TI2V** and **Wan 2.2 14B** T2V/I2V.
- **LTX 2.3** T2V/I2V plus **ID-LoRA** (TalkVid).
- **HunyuanVideo 1.5** 720p T2V/I2V.
- **HunyuanImage 3.0** NF4-v2 T2I via the pinned `Comfy_HunyuanImage3` custom
  node (about 45 GB on disk; see the license note below).
- **Z-Image Turbo** BF16 T2I via the native ComfyUI template.

The HunyuanImage node is CC BY-NC 4.0 and the model carries Tencent's
Hunyuan community license; review both licenses before use. Use the `hunyuan`
loader and generator nodes with `models/hunyuan/HunyuanImage-3-NF4-v2`. Use
the official `image_z_image_turbo` template for Z-Image Turbo.

Files are pinned in the seed script's manifest, downloaded only during the
operator staging step, and verified against SHA-256. LTX checkpoint files are
under `checkpoints/`; LoRAs, vision encoders, and latent upscalers are under
their normal ComfyUI model directories.

### Operator-supplied extra models

The built-in seeder is fixed. To stage an additional, deployment-specific model
set, run:

```bash
./run-playbook.sh admin/install-comfyui-extra-models.yml
```

The play targets the `nvidia` group and selects the host from private host_vars
using `comfyui_vm_enabled`, then uses `comfyui_vm_name` for the VM. No hostname
or VM name is hardcoded. The manifest path defaults to
`comfyui_extra_models_manifest` in host_vars; override it with
`-e comfyui_extra_models_manifest=/path/to/extra-models.tsv`. Use
`-e comfyui_extra_models_download=false` for a verify-only run.

Manifest rows are tab-separated:

```text
id  repo  revision  repo_path  destination_relative_path  sha256
```

The manifest and model files stay outside the public repo. Destination paths are
relative to the VM's model root and are rejected if they escape it. Gated Hugging
Face repos use `HF_TOKEN` from the controller environment; set
`comfyui_hf_token` to override it explicitly.

## Troubleshooting: CUBLAS_STATUS_ALLOC_FAILED

If H3 (or HunyuanImage 3.0) generation fails with `CUDA error:
CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate(handle)` — especially
when it works once after a container restart, then fails instantly on
subsequent runs — the `--disable-cuda-malloc` flag is missing from the
container args. This is already applied in
`config/ansible/admin/templates/comfyui-vm-inner.sh.j2` and survives playbook
re-runs; re-run `admin/install-comfyui-vm.yml --limit <host>` if the flag is
somehow absent. Do not remove the flag: the failure it prevents is
deterministic once the HunyuanImage3 node's memory manager is active in the
process.

## Output

Generated videos and audio are written to `output/` inside the container,
which is `/near1/comfyui/output` on the NAS (durable, shared). The UI's
history/preview shows them as usual.

First sampling run compiles triton CUDA kernels and is slow (minutes of JIT);
that one-time cost then pays off for subsequent runs.
