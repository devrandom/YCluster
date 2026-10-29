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

### Networking: `--network=none` + Unix domain socket + host socat bridge

The container has **no netns interfaces at all** — no AF_INET socket
can ever bind, so a crafted PDF that triggers an external fetch (pypdf
/ pdfplumber resolving external image URIs, font loaders, etc.)
fails at the syscall layer instead of relying on mineru's
application-level SSRF mitigation in
`mineru/cli/public_http_client_policy.py` (which only blocks
attacker-supplied `server_url` / `*-http-client` backends).

Flow:

```
client → http://<host>.xc:8000/file_parse
       → socat TCP-LISTEN:8000,reuseaddr,fork
       → /run/mineru/mineru.sock (UDS, filesystem bind, no AF_INET)
       → mineru-api inside the container
```

Why this works under `--network=none`:

- UDS bind is `bind(AF_UNIX, ...)` — filesystem syscall, no
  netns required.
- mineru-api's only network listener is the UDS; outbound
  `socket(AF_INET, ...)` calls fail with `ENETUNREACH` (no netns
  interfaces), so any external fetch attempt by the parser dies
  immediately.
- A host-side `socat` bridge (`mineru-podman-proxy.service`)
  forwards host TCP:8000 to the UDS. The cluster-internal reach
  model is unchanged — `local-ai-proxy` + nginx still hit
  `http://<host>.xc:8000`.

Why not just `-p 8000:8000` with the default bridge:

- Bridge networking would expose the container's network namespace,
  defeating the SSRF hardening goal.
- `--network=host` shares the host's interfaces — same problem,
  worse blast radius (the container could probe any host the host
  can reach, including the cluster's storage VIP, etcd, etc.).
- UDS + socat is kernel-enforced: nothing to maintain beyond the
  service unit. No firewall rules, no iptables, no nftables.

The UDS path (`/run/mineru/mineru.sock`) is owned by the host's
`dev` user — `install-mineru.yml` pre-creates `/run/mineru/` so the
container's mineru process (running as the same UID via
`--userns=keep-id --user`) can create the socket file there. The socat
process runs as root, connects with the default 0644 socket mode.

Trade-offs:

- **Two services instead of one** (container + proxy). The proxy is
  thin (one socat child per connection) and Restart= keeps it alive
  across mineru-podman restarts.
- **UDS path collision on restart** (stale socket from a crash).
  `/run/mineru/` is host-side tmpfs bind-mounted into the container
  (so the UDS survives across container starts). A crash that exits
  before uvicorn cleans up the socket leaves a stale file on the
  host — the next `podman run` would hit a bind collision. uvicorn
  0.51.0 silently hangs at "Waiting for application startup" on UDS
  collision rather than raising EADDRINUSE (it doesn't surface this
  in a useful way for systemd to react), so the wrapper
  (`run-mineru-uds.py`) does a pre-flight check: `connect()` to the
  existing socket, unlink it if no one's listening (stale file from
  a crash), exit 3 with `ss -lx | grep mineru.sock` if another
  listener is active. Non-socket files at the path are refused
  outright — the wrapper never unlinks non-sockets (silent data
  loss is worse than a loud failure).
- **`--network=none` blocks the warmup step too** — the first-time
  weight download via `mineru-models-download` needs to reach
  HF/ModelScope. The warmup is run as a separate `podman run` that
  deliberately does NOT pass `--network=none` (it's a one-shot CLI
  with no AF_INET bind, no SSRF surface). Subsequent restarts use
  `MINERU_MODEL_SOURCE=local` and never need network.

### GPU access: pinned `--device nvidia.com/gpu=0`

Uses NVIDIA CDI (Container Device Interface) — the modern replacement
for the legacy `--gpus` flag. The CDI specification at
`/etc/cdi/nvidia.yaml` is auto-generated by `nvidia-ctk`. MinerU is pinned to
a single host-visible card (CDI index `0`, plus `CUDA_VISIBLE_DEVICES=0`) so
it cannot grab a card assigned to another workload. The vLLM scheduler
therefore only ever sees that card; any Nemotron co-tenant is pinned to a
different CDI index, or shares CDI 0 only with an explicit reduced budget.

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
│  /run/mineru/mineru.sock      ──┐ │  (UDS, owned by dev)       │
│                                 │ │                            │
│  mineru-podman-proxy.service   │ │  systemd unit (socat)      │
│    └─ socat TCP-LISTEN:8000,reuseaddr,fork   │ │              │
│         └─ UNIX-CONNECT:/run/mineru/mineru.sock               │
│                                 │ │                            │
│  mineru-podman.service          │ │  systemd unit              │
│    └─ podman run --network=none │ │                            │
│         ├─ image: registry.xc:5000/mineru:3.4.3-all-vllm      │
│         ├─ --device nvidia.com/gpu=all                        │
│         ├─ -v /home/dev/.cache/huggingface ──┐                │
│         ├─ -v /home/dev/.cache/paddleocr ────┤                │
│         ├─ -v /run/mineru:/run/mineru ──────┘                │
│         └─ entrypoint: run-mineru-uds.py (uvicorn UDS patch) │
│                                               │                │
│         ┌─────────────────────────────────────┘                │
│         ▼                                                     │
│  ┌──────────────────────────────────────────┐                │
│  │ Container (--network=none, no netns)      │                │
│  │                                           │                │
│  │  mineru-api (FastAPI) on UDS              │                │
│  │    /run/mineru/mineru.sock                │                │
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

## Post-deployment fixes (rolled up into the redeploy)

The initial redeploy hit five bugs in sequence, each masked by the
previous one. They are listed here so the next operator doesn't
re-debug them, and so the playbook's invariants stay visible. A
sixth bug surfaced after the redeploy was declared done — it's
listed at the bottom of this section as "Found after redeploy".

### 1. `/run/mineru` ownership under systemd (`install -d` vs `RuntimeDirectory=`)

systemd 255.4 (Ubuntu 24.04) doesn't honour `RuntimeDirectoryUser=`
/ `RuntimeDirectoryGroup=` (logs `Unknown key name` and silently
ignores them), so `RuntimeDirectory=mineru` creates `/run/mineru`
owned by the unit's `User=root`. The original `ExecStartPre=
/usr/bin/install -d -o dev -g dev -m 0755 /run/mineru` was supposed
to fix that, but empirically on this host the dir stays `root:root`
under systemd — `install -d` skips `-o/-g` on a pre-existing
directory (it only applies ownership on creation), and a later
`systemd-run --scope` (the pull/run ExecStartPre/ExecStart)
triggers an internal re-check that re-pins ownership to root.

Fix: drop `RuntimeDirectory=` entirely; let the `ExecStartPre=
/usr/bin/install -d -o dev -g dev -m 0755 /run/mineru` create the
dir itself (so `-o/-g` is honoured on creation). `/run` is tmpfs,
so this fires on every boot automatically.

### 2. Containerfile `chmod` EPERM as `USER dev`

`COPY run-mineru-uds.py /usr/local/bin/mineru-api-uds` then
`RUN chmod 0755 …` after `USER dev`. `COPY` defaults to
`root:root` ownership, so `dev` can't `chmod` it (`EACCES`).

Fix: `COPY --chown=dev:dev …` — the wrapper then belongs to `dev`
and the `chmod` runs cleanly.

### 3. Wrapper shebang `#!/usr/bin/env python3` → no `python3` in image

The Containerfile installs `python3.12-dev` and `python3.12-venv`
but not the `python3` meta-package, so there is no `/usr/bin/python3`
symlink. `#!/usr/bin/env python3` fails with `env: 'python3': No
such file or directory`, exits 127.

Fix: `#!/usr/bin/python3.12` (absolute path — `python3.12` is
always present from the `python3.12-dev` install).

### 4. Python spawn guard on vllm `AsyncLLM`

vllm's `AsyncLLM` uses `multiprocessing.spawn` to launch the
`EngineCoreProc` subprocess. Spawn re-imports the parent's
`__main__` module to obtain the target callable. The wrapper ran
`main()` unconditionally at module load (no `if __name__ ==
"__main__":` guard), so re-importing the wrapper re-ran `main()`
→ `preload_vlm_model()` → `AsyncLLM.__init__()` → spawn again —
infinite recursion, detected by Python's `_check_not_importing_main()`
as `RuntimeError: An attempt has been made to start a new process
before the current process has finished its bootstrapping phase.`

Fix: wrap `main()` in `if __name__ == "__main__":`. The pre-flight
check and the uvicorn monkey-patch still run at import time, which
is what we want — any process that imports the wrapper (including
vllm-spawned children that happen to start a uvicorn for their own
serving API) gets the patched uvicorn and binds the UDS instead of
TCP.

### 5. Deploy-time invariant: runtime UID matches host `dev`

The Containerfile creates a default `dev` user at UID 1002, but the
systemd unit overrides it with `--user {{ mineru_run_uid }}:{{
mineru_run_gid }}` (defaults 1002). The unit bind-mounts
`/home/dev/*` from the host, and `--userns=keep-id` preserves numeric
IDs, so the runtime UID/GID must match the host's `dev` user. A host
that provisions `dev` as UID 1000 sets both vars in host_vars;
otherwise MinerU sees `EACCES` on every HF cache read and
`mineru.json` parse. `install-mineru.yml` enforces this at deploy time
with `Read host dev UID/GID` + `Pre-flight check that container
runtime IDs match host dev`, failing loudly before podman starts.

### 6. Found after redeploy: `BrokenProcessPool` on `/file_parse`

After fixes 1-5 were deployed and `/health` + the validation paths
(422 / MIME 400) returned 200, the first real PDF parse failed in
~28ms with `HTTP 409` and an error like:

```
A process in the process pool was terminated abruptly while the
future was running or pending.
```

Stack:

```
mineru/cli/common.py:717 do_parse → _process_pipeline →
pipeline_analyze.py:240 doc_analyze_streaming →
load_images_from_pdf_doc → _load_images_from_pdf_bytes_range:324
    images_list = future.result()  # raises BrokenProcessPool
```

Root cause: the UDS pre-flight check (added in fix #1 above) runs
at module-import time. mineru's render executor is created lazily
on the first `/file_parse`, and when it spawns a worker the
worker re-imports the wrapper via Python's spawn import-protection
machinery. By that point the parent's uvicorn has already bound
`/run/mineru/mineru.sock`, so the pre-flight's "connect succeeds →
sys.exit(3)" branch fires and the worker dies silently — the
parent sees BrokenProcessPool. The macOS deployment didn't hit
this because macOS uses Python 3.13 (no vllm, no ProcessPoolExecutor
spawn flow for render — different code path). Operator
verification: 1000+ PDF parses on macOS worked; the first real
PDF parse on Linux failed.

(The earlier "force spawn" hypothesis — `multiprocessing.set_start_method("spawn", force=True)` —
turned out to be unnecessary on top of the pre-flight fix. The
parent's render executor works fine on spawn, fork, or forkserver;
what kills the worker is the pre-flight running at import time
inside the worker. The spawn-set line was tried first and left a
useful breadcrumb in git history, but the actual fix is the
pre-flight guard below.)

Fix: move `_preflight_uds(UDS_PATH)` inside the `if __name__ ==
"__main__":` guard in `run-mineru-uds.py`. The uvicorn monkey-patch
stays at import time (so any child process that starts a uvicorn
for its own serving API still binds the UDS). The pre-flight only
runs in the FastAPI server process where it actually matters
(rejecting a duplicate-start). Verified by the live `/file_parse`
POST against `nv2` after the rebuild returning `status=completed`
in ~2 s.

### 7. Layer separation so wrapper changes don't push 11 GB

The wrapper sits at the very end of the Containerfile (the last
`COPY --chown=dev:dev run-mineru-uds.py …` step, then a tiny
`chmod`), but `buildah bud` in 1.33 squashes by default — the
resulting image has just two OCI layers (ubuntu base + a single
~11 GB layer that contains everything else, wrapper included). Any
change to the wrapper therefore invalidates the whole ~11 GB layer
and the push to `registry.xc` re-uploads all of it (~5-6 min over
the cluster network).

Fix: `buildah bud --layers …` in `install-mineru.yml`. `--layers`
keeps each Containerfile step as its own OCI layer; the resulting
manifest has ~9 layers (verified):

```
layer 0:  30 MB   ubuntu:24.04 base
layer 1: 372 MB   apt deps (ffmpeg, libgl*, fonts-noto-cjk, gcc, python3.12-dev)
layer 2:   ~0 MB  uv installer
layer 3:  26 MB   (uv pip install warmup)
layer 4: 5430 MB  mineru[all] + torchvision + vllm 0.11.x  (the big one)
layer 5:  12 MB   groupadd/useradd --uid 1002 dev
layer 6:  ~0 MB   COPY --chown=dev:dev run-mineru-uds.py
layer 7:  ~0 MB   chmod 0755 + ENV MINERU_MODEL_SOURCE
layer 8:  ~0 MB   ENTRYPOINT + CMD  (metadata)
```

Now a wrapper-only change invalidates layers 6-8 (a few KB total)
and the push is essentially instant. The big vllm/mineru layer
(layer 4) is reused from the registry on the next `podman pull` in
ExecStartPre.

The OCI spec already deduplicates layers by content hash; this
change just makes the per-step layers visible to that dedup. The
playbook's rebuild-detect task (HEAD on the manifest tag) still
needs a manual manifest digest delete to force a rebuild — the
follow-up TODO is to compare layer digests instead.

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
| new | `config/ansible/admin/files/mineru/run-mineru-uds.py` | uvicorn monkey-patch launcher; binds the API on `/run/mineru/mineru.sock` instead of TCP; pre-flight checks the UDS path for stale files vs. live listeners (uvicorn 0.51.0 silently hangs on UDS collision, so the wrapper fails fast with a diagnostic) |
| new | `config/ansible/admin/files/mineru/mineru-podman.service` | systemd unit wrapping `podman run --network=none ...` |
| new | `config/ansible/admin/files/mineru/mineru-podman-proxy.service` | systemd unit running `socat TCP-LISTEN:8000,fork UNIX-CONNECT:/run/mineru/mineru.sock` |
| edit | `config/ansible/admin/install-mineru.yml` | apt `socat`, pre-create `/run/mineru/`, install + start proxy unit, stage UDS wrapper |
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
| Stale UDS after container crash blocks restart | The wrapper (`run-mineru-uds.py`) does a pre-flight check: `connect()` to the existing socket, unlink if no one's listening (stale), exit 3 with `ss -lx` diagnostic if another listener is active. Necessary because uvicorn 0.51.0 silently hangs on UDS bind collision rather than raising EADDRINUSE, which would otherwise wedge systemd's `Restart=always` loop. Non-socket files at the UDS path are refused outright (wrapper never unlinks non-sockets — fails loudly to avoid silent data loss). |
| socat dies and mineru-podman stays up | proxy unit has `Restart=always` and `After=mineru-podman.service`; orchestrator restart covers the reverse |
| Container crashes silently (no AF_INET, no external probe) | `/health` polling via the proxy's TCP:8000 still surfaces 503 on UDS miss; operator-visible from `journalctl -u mineru-podman-proxy` |

## Open questions / future work

- **vLLM `gpu_memory_utilization` override**: MinerU auto-defaults to 0.5
  (`set_default_gpu_memory_utilization()`), which pinned ~48 GB of the 96 GB
  card to a KV cache far larger than document parsing needs. The unit now
  overrides it with `--gpu-memory-utilization 0.2 --max-model-len 8192` (both
  flow through to `AsyncLLM` since `mineru-api` accepts extra CLI args),
  dropping idle footprint from ~48 GB to ~20 GB and restoring headroom for a
  co-tenant. `--max-model-len` must stay ≤ 8192 (this VLM's
  `max_position_embeddings`); see
  `todo/archive/mineru-gpu-memory-footprint.md`.
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
