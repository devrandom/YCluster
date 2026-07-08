# MinerU Podman Refactor

## TL;DR

Move MinerU on Nvidia hosts from a bare `uv`-installed venv (systemd,
`mineru.service`) to a podman container (`mineru-podman.service`). Image
gets pushed to `registry.xc:5000/mineru:VERSION` so all Nvidia hosts
share the same artifact. The container ships `mineru[all] + torchvision
+ vllm` — all three MinerU backends (pipeline / vlm / hybrid) become
runtime-switchable via the `backend` form field. vLLM gives proper
page-level concurrency (3-5× speedup on the per-block extract pass).

The old `mineru.service` (venv) stays in place as a one-command
rollback target. It is stopped on hosts that switch to the container;
re-enabling it puts the host back on the previous behavior without
reinstalling anything.

## Context

Current state (as of this design):

- The first Nvidia host runs MinerU 3.4.3 from a venv at `/home/dev/mineru-venv`
  (uv-installed
  `mineru[vlm] + torchvision`). Systemd unit `mineru.service` starts
  `mineru-api --host 0.0.0.0 --port 8000 --enable-vlm-preload true`.
- HF model cache at `/home/dev/.cache/huggingface` (2.2 GB VLM model).
- Default backend is `transformers`. 2-page real doc with 43 text
  blocks: 7.5 s VLM pass + 169 s sequential extract = ~2:58 wall clock.
- `hybrid-engine` is unavailable on this install because `[vlm]` lacks
  the pipeline deps (paddle / onnxruntime).
- vLLM is not installed; `vllm-async-engine` (which would parallelise
  page-level extract) is therefore unreachable.
- The mineru package + mineru_vl_utils + transformers + torch + torchvision
  are scattered across the system venv with `uv`; upgrades require
  reinstalling the whole venv and restarting the unit.

What this refactor changes:

1. **Containerise** the install — image is a single artifact, version
   tag, push-once-pull-many. Upgrades are `podman pull && systemctl
   restart mineru-podman`.
2. **Ship `mineru[all]`** so `hybrid-engine` is selectable at runtime.
   Paddle / onnxruntime stay loaded in the container (cheap; CPU-bound).
3. **Ship vLLM** so `vllm-async-engine` is auto-selected on Linux
   (`get_vlm_engine('auto', is_async=True)`). Pages process in
   parallel via vLLM's scheduler instead of stepping through blocks
   one at a time.
4. **Push to registry** so any future Nvidia host can pull instead
   can pull the same artifact and skip the build.

The proxy stays transparent — it forwards the multipart body
unchanged. Clients that want a non-default backend send
`backend=pipeline|vlm-engine|hybrid-engine` in the form data. The
`contrib/inference ocr --backend ...` CLI sends it by default;
raw curl / UI callers must include it explicitly.

## Decisions

### Container runtime: podman (not docker)

Nvidia hosts have podman 4.9.3 + buildah + NVIDIA Container Toolkit 1.19
+ CDI at `/etc/cdi/nvidia.yaml`. Docker is not installed. Podman is
rootless-capable (we run as the `dev` user), is the project's existing
container tooling on Nvidia nodes, and is daemonless.

### Image registry: `registry.xc:5000`

The cluster's Docker registry (storage-leader-local, on the storage
VIP 10.0.0.100:5000) is already in use for Open-WebUI and other
services. Pushing the mineru image there:

- Other Nvidia hosts can pull instead
  of rebuilding (~10 min saved per host).
- Version is tracked by tag (`mineru:3.4.3-all-vllm`).
- Re-deploys are `podman pull registry.xc:5000/mineru:VERSION` instead
  of `uv pip install` from scratch.

Image size will be ~10 GB. The registry has enough disk; we monitor
usage and prune old tags in the playbook.

### Containerfile base: `docker.io/library/ubuntu:24.04`

Same base across all hosts (consistent glibc, Python 3.12). Layered:

1. `apt-get install` system deps (`ffmpeg libgl1 libglib2.0-0
   fonts-noto-cjk python3.12-venv`).
2. `pip install uv` (single small layer).
3. `uv pip install --system 'mineru[all]' torchvision vllm` (the big
   layer — caches well across builds).
4. Pre-download paddle OCR weights at build time (saves ~1 GB download
   on first request).
5. `ENTRYPOINT ["mineru-api", "--host", "0.0.0.0", "--port", "8000",
   "--enable-vlm-preload", "true"]`.

System deps rationale:

- `ffmpeg` — MinerU uses it for some media preprocessing.
- `libgl1 libglib2.0-0` — OpenCV / Pillow deps.
- `fonts-noto-cjk` — CJK character rendering (matches the macOS
  install which has CJK fonts built in).
- `python3.12-venv` — fallback in case uv needs it; not strictly
  required since uv manages its own envs.

### Networking: port mapping (`-p 8000:8000`), not `--network=host`

Port mapping:

- Keeps container network isolated from the host (defense in depth).
- nginx / systemd-resolved on the host still work normally.
- Easier to swap: stop one container, start another, no host network
  state to manage.
- Allows future migration to rootless podman with slirp4netns without
  changing the unit file.

`--network=host` would skip the port mapping but loses isolation and
makes future moves to other runtimes harder. Not worth the convenience.

### GPU access: `--device nvidia.com/gpu=all`

Uses NVIDIA CDI (Container Device Interface) — the modern replacement
for the legacy `--gpus` flag. The CDI specification at
`/etc/cdi/nvidia.yaml` is auto-generated by `nvidia-ctk`. With
`--device nvidia.com/gpu=all` the container sees both Blackwell GPUs
and `nvidia-smi` works inside.

Both GPUs are passed through because the vLLM scheduler may pick
either based on `gpu_memory_utilization`; passing only one would
constrain future tuning.

### HF cache + PaddleOCR cache: bind-mounted from host

```
-v /home/dev/.cache/huggingface:/home/dev/.cache/huggingface
-v /home/dev/.cache/paddleocr:/home/dev/.cache/paddleocr
```

Why bind-mount rather than baking into the image:

- Image stays small (~10 GB instead of ~13 GB with weights).
- Cache survives `podman rm` — no re-download on every redeploy.
- Multiple hosts share the cache via Ceph-rbd `/home/dev` (the `dev`
  user's home is on cluster storage).
- The pre-download step at build time only handles paddle weights;
  HuggingFace weights are pulled on first request (as before).

Mount is `:ro` once populated, `:rw` during first run so the container
can write the paddle cache. The systemd unit handles this with a
one-shot `podman run --rm` "warmup" step before starting the long-
running service.

### vLLM in the image: yes

vLLM provides:

- **Proper batching**: `vllm-async-engine` calls `aio_batch_predict`
  which schedules requests through vLLM's continuous batching engine
  instead of one-at-a-time `model.generate` calls.
- **PagedAttention**: KV cache is paged across GPU memory, so larger
  effective batch sizes without OOM.
- **CUDA graphs**: forward-pass kernels precompiled for the model,
  shaving ~1 ms per call.

For our 2-page doc with 43 blocks, expected wall-clock improvement:
`stepping` mode (current, transformers) 169 s extract → `concurrent`
mode (vllm-async) ~30-60 s extract. Total ~45-90 s instead of ~2:58.

Cost: +5-10 GB in the image (vllm + cuda toolkit deps). Worth it for
the speedup and the upgrade path (vllm is the upstream MinerU
recommendation for Linux).

### Three backends runtime-switchable: yes

The MinerU dispatcher at `common.py:725-757` already branches on the
`backend` form field. The container ships everything; the client picks:

- `backend=pipeline` — pure OCR/layout (no VLM); for scanned docs
  where VLM hallucinates text. Slowest but most general.
- `backend=vlm-engine` — VLM only; what we ship today. Fast, good
  for text-heavy docs with simple layout.
- `backend=hybrid-engine` — VLM + pipeline sidecars. Best quality on
  edge cases (handwriting, dense tables, formulas). Slower than
  vlm-engine (~30 s extra pipeline overhead at `medium` effort;
  ~60 s at `high`).

The proxy is transparent — it forwards the `backend` field
unchanged. Clients pick at request time; if `backend` is absent,
MinerU's own default (`hybrid-engine`) kicks in. That means
callers using `mineru[all]` who omit `backend` will get
`hybrid-engine` by default, which is slow but safe. Callers
wanting `vlm-engine` must send `backend=vlm-engine` explicitly
(our `contrib/inference ocr` CLI does this by default).

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Host (Nvidia)                                                 │
│                                                               │
│  /home/dev/.cache/huggingface/  ──┐  (HF model cache, ~2.2 GB)│
│  /home/dev/.cache/paddleocr/   ──┤                            │
│                                  │                            │
│  mineru-podman.service            │  systemd unit              │
│    └─ podman run ...              │                            │
│         ├─ image: registry.xc:5000/mineru:3.4.3-all-vllm      │
│         ├─ -p 8000:8000           │                            │
│         ├─ --device nvidia.com/gpu=all                        │
│         ├─ -v /home/dev/.cache/huggingface ──┐                │
│         ├─ -v /home/dev/.cache/paddleocr ────┤                │
│         └─ entrypoint: mineru-api            │                │
│                                               │                │
│         ┌────────────────────────────────────┘                │
│         ▼                                                     │
│  ┌──────────────────────────────────────────┐                │
│  │ Container                                 │                │
│  │                                           │                │
│  │  mineru-api (FastAPI) on 0.0.0.0:8000     │                │
│  │    ├─ MinerUClient (VLM)                  │                │
│  │    │   backend=vllm-async-engine         │                │
│  │    │   batching_mode=concurrent          │                │
│  │    │   vllm_async_llm = AsyncLLM(...)     │                │
│  │    └─ hybrid_pipeline_model (paddle/onnx) │                │
│  │        ├─ layout detection                │                │
│  │        ├─ OCR (ppocr)                     │                │
│  │        ├─ formula recognition             │                │
│  │        └─ table recognition               │                │
│  └──────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────┘
```

## Deployment flow

### Playbook (`install-mineru.yml`)

1. **Ensure podman + NVIDIA CDI** (apt: `podman`, `nvidia-container-toolkit`,
   `cdi`; `nvidia-ctk cdi generate` if missing).
2. **Build image on the host** (`buildah bud -f Containerfile -t
   mineru:VERSION .`) — only if not already cached or version changed.
   Layered to cache the big `uv pip install` step across rebuilds.
3. **Push to registry** (`podman push mineru:VERSION
   registry.xc:5000/mineru:VERSION`).
4. **Pre-create cache dirs** (`/home/dev/.cache/huggingface`,
   `/home/dev/.cache/paddleocr`) so the bind-mount target exists.
5. **Warmup run** — one-shot `podman run --rm` to trigger paddle OCR
   model download (cached in the bind-mounted dir for next runs).
6. **Install systemd unit** (`mineru-podman.service`) which runs
   `podman run` with the args above.
7. **Stop old `mineru.service`** (the venv-based unit). Leave the
   unit file in place for rollback.
8. **Start `mineru-podman.service`**.
9. **Smoke test** — POST `/file_parse` with a tiny PDF on each of
   the three backends, expect 200.

### macOS hosts (`macos/setup-mineru.yml`)

Unchanged. Apple Silicon path uses `[all]` via the existing uv
playbook; the container refactor is Nvidia-only. macOS stays on
MLX + hybrid because that's the best fit for the platform.

### Future Nvidia hosts

Adding additional Nvidia boxes becomes:

1. Set `mineru_enabled: true` in `host_vars/<host>.yml`.
2. Run `install-mineru.yml --limit <host>`.
3. Playbook pulls the image from the registry (already built),
   bind-mounts `/home/dev/.cache` (already on Ceph-rbd), starts
   the service. ~2 min per host vs ~15 min if rebuilding.

## Rollback

If the container fails on a host:

```bash
ssh "$YC_SYNC_HOST" "ssh <nvidia-host> 'systemctl stop mineru-podman && \
  systemctl start mineru && \
  sleep 2 && curl -sS http://127.0.0.1:8000/health'"
```

The old `mineru.service` (venv-based) is left installed. It starts
the same `mineru-api` on the same port 8000 with the same model
cache. The proxy's existing URL for that host stays valid.

The proxy doesn't need to change — it polls `/health` (which the
venv-based service also exposes) and routes to the healthy backend.

## Files

| Action | Path | Purpose |
|---|---|---|
| new | `config/ansible/admin/files/mineru/Containerfile` | Ubuntu 24.04 + uv-installed `mineru[all] torchvision vllm` |
| new | `config/ansible/admin/files/mineru/mineru-podman.service` | systemd unit wrapping `podman run` |
| edit | `config/ansible/admin/install-mineru.yml` | build image, push, warmup, start container |
| keep | `config/ansible/admin/files/mineru/run-mineru.sh` | unused after refactor; leave for reference |
| keep | `config/ansible/admin/files/mineru/mineru.service` | rollback path; not auto-started on Nvidia hosts after refactor |
| edit | `docs/operations/document-extraction.md` | deployment notes: podman, three backends, registry, HF bind-mount |
| edit | `docs/usage/document-extraction.md` | usage: clients send `--backend`; proxy stays transparent |

## Risks

| Risk | Mitigation |
|---|---|
| Build fails on the Nvidia host (uv pip resolver choke on paddle + vllm combo) | pin versions in `Containerfile`; test build in dry-run before commit |
| Container doesn't start (CDI config drift) | `nvidia-ctk cdi generate` runs in playbook before image build |
| Models re-download despite bind-mount | playbook asserts cache dirs exist with `du -sh`; warmup run verifies paddle weights |
| Registry disk fills (~10 GB image × N versions) | playbook prunes tags older than 30 days; monitor via Prometheus |
| vLLM + MinerU version skew (vllm pins a torch version MinerU dislikes) | pin vllm version to one known-good combo; document in Containerfile |
| Podman rootless vs rootful (image needs root for some paddle init) | run unit as `dev` (rootless); if paddle needs root, document the fallback |

## Open questions / future work

- **vLLM `gpu_memory_utilization`**: currently auto-set to 0.9 by
  `set_default_gpu_memory_utilization()` in MinerU. With 95 GB VRAM
  per GPU that's fine; tune if we ever need to share with another
  service on the same GPU.
- **Cross-page table merge** (`enable_cross_page_table_merge=True`):
  adds an extra vLLM call per doc. May want to disable for short
  docs to skip the overhead.
- **Image tag strategy**: `mineru:MAJOR.MINOR.PATCH-all-vllm` (e.g.
  `mineru:3.4.3-all-vllm`). Auto-bump on MinerU upstream release;
  rebuild playbook runs `uv pip install --upgrade mineru` first.
- **macOS parity**: should the macOS playbook also adopt `[all]` +
  vllm is N/A (no vllm on macOS). macOS already has `[all]`. Leave
  alone.
- **vLLM + MLX parity test**: confirm that `backend=vlm-engine`
  produces equivalent output across vllm-async-engine (Linux) and
  mlx-engine (macOS). Sample a few real docs through both paths.

## See also

- [`mineru.md`](mineru.md) — parent design doc (technology choice,
  why MinerU, why MLX on Mac)
- [`docs/operations/document-extraction.md`](../operations/document-extraction.md) —
  deployment + ops + troubleshooting (will be updated alongside this refactor)
- [`docs/design/virtual-test-environment.md`](virtual-test-environment.md) —
  the throwaway local Incus cluster where this can be tested before
  touching prod Nvidia hosts
