# Document Extraction (PDF / DOCX / PPTX / XLSX via MinerU)

## TL;DR

- **Service**: [MinerU 3.4.x](https://github.com/opendatalab/MinerU) — OpenDataLab's
  layout-aware document parser. Converts PDFs and office documents to
  structured markdown + per-page layout JSON.
- **Server**: built-in `mineru-api` FastAPI server (since 3.0.0) on port 8000.
  No community wrapper needed; the official server supersedes all of them.
- **Compute**: Apple Silicon via the `mlx` extra (since 3.0). MLX gives
  ~220% speedup on text PDFs vs. MPS. M3 Ultra (512 GB unified memory, 80 GPU
  cores) hosts both TEI (bge-m3 embeddings) and MinerU (document parsing).
- **Routing**: registered as a backend with `local-ai-proxy`; clients hit
  `https://inference.xc/v1/file_parse` like any other model. The proxy
  reads the `model` form field from the multipart body and rewrites the
  path to MinerU's native `/file_parse` (parallels the existing
  `/v1/tokenize → /tokenize` rewrite).
- **Auth**: same gateway `auth_request` as the rest of inference (master
  key + Open-WebUI api_key). MinerU itself listens with no API key.
- **Deployment**: `config/ansible/macos/setup-mineru.yml`, gated on
  `mineru_enabled: true` in `host_vars/<node>.yml`. Daemonised via
  LaunchDaemon `com.ycluster.mineru`.

For the deployment playbook, host opt-in, registration, and ops/troubleshooting,
see [`docs/operations/document-extraction.md`](../operations/document-extraction.md).
This page covers the technology choice and design only.

## Why MinerU

The cluster needs a PDF/document parser for RAG ingestion and bulk
extraction. Requirements:

1. Layout-aware (preserves reading order, headings, tables, equations).
2. Runs on Apple Silicon natively (we already pay for three M3 Ultras).
3. OpenAI-style HTTP API so it slots behind `local-ai-proxy` like
   everything else.
4. Acceptable license (Apache-2.0-based since 3.1.0; safe for closed use).

MinerU 3.x hits all four. The 2.x `magic-pdf` line is frozen at 1.3.12
on PyPI; 3.x is the live stack.

Alternatives considered:

| Option | Verdict |
|---|---|
| **MinerU 3.x (chosen)** | Built-in `mineru-api` FastAPI server, MLX on Mac, Apache-based license, VLM + pipeline hybrid backend. |
| Unstructured.io | Python; needs custom FastAPI shim for OpenAI-style multipart; slower than MinerU on layout-heavy docs. |
| Marker (datalab-to) | Python; single-process CLI, no built-in HTTP server. |
| Docling (IBM) | Python; heavier deps, no first-class Metal/MLX backend. |
| Tesseract + custom pipeline | DIY; layout preservation is manual work. |

## Why not Docker

MinerU's official Docker image (`vllm/vllm-openai:v0.21.0`) works on Linux
and Windows-WSL2, **but is explicitly warned against on macOS**: Docker-on-Mac
can't reach MPS or MLX acceleration, so the Metal backend is silently
disabled and inference falls back to CPU. We stay with `uv pip install
"mineru[all]"` (the `[all]` meta-extra auto-picks `core + s3 + mlx` on
darwin) and run it as a native Python service under a LaunchDaemon, same
pattern as `setup-tei.yml`.

## API shape

Clients reach the endpoint through `local-ai-proxy` like any other
model — there is no separate document-parsing URL. The proxy reads the
`model` form field from the multipart body, looks up the backend in etcd,
and rewrites the path from `/v1/file_parse` to MinerU's native
`/file_parse` before dispatching:

```
POST https://inference.xc/v1/file_parse
Authorization: Bearer <cluster-token>          # Open-WebUI api_key, validated by the proxy
Content-Type: multipart/form-data; boundary=…

-F "files=@report.pdf"
-F "model=document-parse"
-F "return_md=true"
```

(or `http://inference.xc/v1/file_parse` for cluster-internal callers.)

### Backend routing

```
client → http://inference.xc/v1/file_parse (nginx + auth_request)
       → local-ai-proxy :4001               (model form-field routing)
       → http://m2.xc:8000/file_parse       (mineru-api, MLX on M3 Ultra)
```

The `/v1/file_parse → /file_parse` rewrite is added to the existing
path-rewrite switch in [`local-ai-proxy/handler.go`](../../local-ai-proxy/handler.go)
(parallels the `/v1/tokenize → /tokenize` and `/v1/detokenize → /detokenize`
rewrites for llama.cpp / vLLM). Test coverage in
[`local-ai-proxy/router_test.go`](../../local-ai-proxy/router_test.go)
(`TestHandlerRewritesTokenizePaths` — extended with a `file_parse` row).

### MinerU's native response

MinerU's `/file_parse` returns a ZIP containing the parsed files for the
uploaded document(s):

| File | Content |
|---|---|
| `<name>.md` | Human-readable Markdown (with embedded image references). |
| `<name>_content_list.json` | Flat reading-order list of content blocks (text/image/table/equation/code/list). |
| `<name>_content_list_v2.json` | Page-grouped `{type, content: {...}}` schema (3.0+). |
| `<name>_middle.json` | Intermediate structured layout (blocks/lines/spans with bboxes). |
| `<name>_model.json` | Raw model output (debug). |
| `<name>_layout.pdf` | Layout analysis visualization (debug). |

Per-file flags (`return_md`, `return_middle_json`, `return_content_list_json`,
`return_model_json`, `return_original_file`) selectively include them.
The whole directory is zipped when `response_format_zip=true`. Outputs
stream back to the client — **no on-disk persistence** (no RBD volume;
the response is the parse).

The MinerU process itself listens on `:8000` with **no API key** —
the cluster proxy is the only auth surface, so a stray LAN caller can't
do useful work without a valid cluster token anyway. Register a new
MinerU node with `ycluster inference add http://m<N>.xc:8000`.

## Model registration

Two model names map to the same `http://m2.xc:8000` backend:

```bash
ycluster inference add http://m2.xc:8000 document-parse
ycluster inference add http://m2.xc:8000 mineru-parse
```

`document-parse` is the semantic / user-facing name; `mineru-parse` is
the implementation-specific alias. Both appear in `inference.xc/v1/models`
and both can be used as the `model` form field.

## Hardware

| Platform | Verdict | Notes |
|---|---|---|
| Apple Silicon Mac Studio (M2/M3/M4 Max or Ultra) | **Works.** | `mineru[all]` pulls the `mlx` extra on darwin (since 3.0). 3.3 release notes cite ~220% speedup on text PDFs vs MPS. M3 Ultra (512 GB unified memory) is the current host. |
| NVIDIA GPU (L4 / A10 / RTX 4090 / A100, Volta+) | **Faster, but vLLM pre-allocates VRAM.** | `mineru[all]` on Linux pulls `vllm>=0.10.1.1`. ~8 GB VRAM minimum for the VLM backend. Run only one `mineru-openai-server` per node (vLLM grabs all visible VRAM). |
| AMD Strix Halo (Ryzen AI Max) | **Not validated.** | No published MinerU-on-ROCm success stories; VLM backend requires CUDA. `pipeline` backend could run on CPU but is impractically slow for batch ingestion. |
| CPU-only | **Viable only for `pipeline` backend.** | VLM/hybrid backends are GPU-only by design. |

Disk footprint on the host:

- `pipeline` backend weights: ~20 GB (layout detection, table
  recognition, formula OCR, PP-OCRv6).
- VLM (`MinerU2.5-Pro-2605-1.2B`): ~2.2 GB.
- HF cache location: `~/.cache/huggingface/` (overridable via
  `MINERU_MODEL_SOURCE=modelscope|local` env var).

3.4 added cache-first download logic, so subsequent installs on hosts
that already have the cache reuse it. Set `MINERU_MODEL_SOURCE=local`
after the initial download to skip the network check on every start.

## Configuration

| Setting | Value | Why |
|---|---|---|
| `--host` | `0.0.0.0` | Must be reachable from `local-ai-proxy` (which runs on the storage leader, a different host). |
| `--port` | `8000` | Distinct from TEI's 8080 on the same Mac. |
| `--enable-vlm-preload` | `true` | Warms the VLM at startup; first request after boot pays ~30–90 s otherwise. |
| `MINERU_API_MAX_CONCURRENT_REQUESTS` | `3` (default) | Conservative because m2 also runs TEI (Metal/RAM contention). |
| `MINERU_API_OUTPUT_ROOT` | unset | Outputs stream back to the client; no on-disk persistence. |
| `MINERU_MODEL_SOURCE` | `local` | After initial `mineru-models-download`, skip network re-check. |

Defaults that we accept as-is:

- `MINERU_PROCESSING_WINDOW_SIZE: 64` — sliding-window pages for long docs.
- `MINERU_API_TASK_RETENTION_SECONDS: 86400` — 24h. Not relevant for sync
  `/file_parse` (we don't use `/tasks`); retained for compat.
- `MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS: 300`.
- `MINERU_TASK_RESULT_TIMEOUT_SECONDS: 3600`.
- `MINERU_TASK_RESULT_DOWNLOAD_TIMEOUT_SECONDS: 600`.
- `MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS: 300`.
- `MINERU_PDF_RENDER_THREADS: 4`.

## Current deployment

- **Host**: m2 (Apple M3 Ultra, 512 GB unified memory, 80 GPU cores, Metal 4)
  — same Mac as the TEI embeddings endpoint.
- **Provisioner**: `config/ansible/macos/setup-mineru.yml` (wired in via `site.yml`).
- **Daemon**: `com.ycluster.mineru` LaunchDaemon under user `dev`.
- **Backend registration**:
  ```bash
  ycluster inference add http://m2.xc:8000 document-parse
  ycluster inference add http://m2.xc:8000 mineru-parse
  ```
- **Reachability**: `https://inference.xc/v1/file_parse` (cluster-internal:
  `http://inference.xc/v1/file_parse`).

## Risks and caveats

- **Cold start ~30–90 s** (MLX VLM preload heavier than TEI's bge-m3).
  `local-ai-proxy`'s health checker marks the backend unhealthy until
  `/health` returns 200; first request after boot pays the tax.
- **m2 contention**: TEI (Candle/Metal) and MinerU (MLX/Metal) share GPU
  and unified memory. Default `MINERU_API_MAX_CONCURRENT_REQUESTS=3` is
  conservative; observe under load and tune if needed. Don't raise above
  6 without measuring.
- **In-process task state**: MinerU tracks `/tasks` results in memory
  only. Service restart loses pending tasks (404 on old `task_id`s).
  Recommended client path is sync `/file_parse` (one-shot); async
  `/tasks` is reserved for very long docs (>1000 pages) where request
  timeout becomes a concern.
- **Leader failover** (if MinerU is moved to a storage-leader topology
  in the future): the in-flight `/tasks` are lost on failover. Sync
  `/file_parse` clients just retry against the new leader.
- **CJK fonts**: macOS already ships them (no install needed). Only a
  gotcha on Linux (`fonts-noto-core fonts-noto-cjk`).
- **No `magic-pdf` command anymore** (3.x split into 5 CLIs). Anything
  scripted against the old binary breaks; the legacy `magic-pdf 1.3.12`
  PyPI package is frozen and unmaintained.
- **License**: changed AGPLv3 → custom Apache-2.0-based in 3.1.0
  (2026-04-18). Read `LICENSE.md` for any CLA-style clauses before
  shipping a SaaS.

## Future work

- **NVIDIA node** (`nv1–nv5`) — drop-in for higher throughput. Same
  `ycluster inference add` pattern. Worth it if `MINERU_API_MAX_CONCURRENT_REQUESTS=3`
  on the Mac becomes a bottleneck under sustained load.
- **Multi-instance via `mineru-router`** — when more than one host runs
  MinerU, register a `mineru-router` (port 8002) as the local-ai-proxy
  backend instead of individual `mineru-api` instances. Provides
  load balancing across workers/GPUs.
- **Async `/tasks` for very long docs** — add a thin client wrapper
  that submits via `/tasks`, polls `/tasks/{id}`, and downloads the
  result ZIP. Useful for >1000-page documents where `/file_parse` would
  hit the nginx `proxy_read_timeout`.
- **Custom body-size tuning** — default `client_max_body_size` is
  200 MiB. Large textbooks can exceed this; bump nginx
  (`inference-internal.conf`) and `local-ai-proxy/router.go`
  (`maxMultipartBodyBytes`) in lockstep.
