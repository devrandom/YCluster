# Embeddings Endpoint (BAAI/bge-m3 via TEI)

## TL;DR

- **Model**: `BAAI/bge-m3` — multilingual, 1024-dim, ~570 M params, ~1.15 GB fp16. Not gated on Hugging Face; no `HF_TOKEN` required.
- **Server**: `text-embeddings-inference` (TEI) — Rust binary, OpenAI-compatible `/v1/embeddings` out of the box. Single-instance per host; scales by adding more nodes.
- **Compute**: Apple Silicon Homebrew formula (Candle/Metal backend). No Docker, so no Docker-on-Mac VM/Metal problem. M3 Ultra (512 GB unified memory, 80 GPU cores) is the sweet spot. NVIDIA is ~2–5× faster for batch ingestion but not currently used.
- **Routing**: registered as a backend with `local-ai-proxy`; clients hit `https://inference.xc/v1/embeddings` like any other model. Auth is the gateway's auth_request (master key + Open-WebUI api_key); TEI listens with no API key of its own.
- **Deployment**: `config/ansible/macos/setup-tei.yml`, gated on `tei_enabled: true` in `host_vars/<node>.yml`. Daemonised via LaunchDaemon `com.ycluster.tei`.

For the deployment playbook, host opt-in, and ops/troubleshooting, see
[`docs/operations/embeddings.md`](../operations/embeddings.md). This page
covers the technology choice and design only.

## Why TEI

The cluster already runs `local-ai-proxy` as a JSON-centric OpenAI-compatible
gateway, so the embedding backend needs to speak `/v1/embeddings` natively.
TEI does that out of the box, and is a single Rust binary — no Python
runtime, no venv, no GPU-driver dance. Candle (the Rust ML framework TEI
uses) ships first-class Metal support for Apple Silicon, so we can install
it from Homebrew and get GPU acceleration without Docker.

Alternatives considered:

| Option | Verdict |
|---|---|
| **TEI (chosen)** | Rust binary, OpenAI API native, Candle/Metal on Mac. |
| sentence-transformers in Python | Needs venv, GPU plumbing, custom FastAPI shim for `/v1/embeddings`. |
| Ollama embeddings | `/v1/embeddings` supported but ollama is being deprecated on macOS nodes (see `setup-macos.yml`); would conflict with the exo stack. |
| llama.cpp `--embedding` | OpenAI-compatible since llama.cpp b3000-ish, but no Metal backend on Apple Silicon (Vulkan/ROCm/CUDA only). |
| FastEmbed (qdrant) | Python; same FastAPI-shim problem as sentence-transformers. |

## Hardware

| Platform | Verdict | Notes |
|---|---|---|
| NVIDIA GPU (L4 / A10 / RTX 4090 / A100) | **Recommended for batch ingestion.** | TEI's `cuda-1.9` image; ~16 GB VRAM is plenty for bge-m3. |
| Apple Silicon Mac Studio (M2/M3/M4 Max or Ultra) | **Works, slower than NVIDIA.** | TEI's Apple-Silicon Homebrew formula ships a Metal backend via Candle — no Docker required. Unified memory is a plus for larger models later. Expect roughly 2–5× slower than an A100 for bge-m3 batch inference — still fine for personal-scale corpora. M3 Ultra / M4 Ultra with ≥64 GB unified memory is the sweet spot. |
| AMD Strix Halo (Ryzen AI Max) | **Not viable for TEI.** | TEI has no ROCm image. Candle's ROCm support is experimental; bge-m3 on CPU via TEI would be impractically slow for batch ingestion. |

VRAM sizing for the planned stack on NVIDIA:

- bge-m3: ~2–4 GB.
- + `bge-reranker-v2-m3` (slow path, later): ~+2 GB.
- + OCR (later, only if Ragflow runs here): ~+4–8 GB.

## API shape

Clients reach the endpoint through `local-ai-proxy` like any other
model — there is no separate embedding URL. The proxy does auth
(Open-WebUI api_key table) and routes by the `model` field in the body:

```
POST https://inference.xc/v1/embeddings
Authorization: Bearer <cluster-token>   # Open-WebUI api_key, validated by the proxy
```

(or `http://inference.xc/v1/embeddings` for cluster-internal callers.)

### Request

```json
{"model": "BAAI/bge-m3", "input": ["text one", "text two"], "encoding_format": "float"}
```

### Response

```json
{"data": [{"index": 0, "embedding": [0.0123, ...]}, {"index": 1, "embedding": [0.0789, ...]}],
 "model": "BAAI/bge-m3", "usage": {"prompt_tokens": N, "total_tokens": N}}
```

`input` may be a string or array of strings. Each embedding has length
**1024**. Clients must chunk ≤32 inputs per request (TEI's
`max_client_batch_size` default).

The TEI process itself listens on `:8080` with **no API key** —
the cluster proxy is the only auth surface, so a stray LAN caller can't
do useful work without a valid cluster token anyway. Register a new TEI
node with `ycluster inference add http://m<N>.xc:8080 BAAI/bge-m3`.

Other endpoints (not needed for v1): `GET /health` (readiness), `POST /embed`
(native, slightly lower overhead than OpenAI path).

## Configuration

| Setting | Value | Why |
|---|---|---|
| `--model-id` | `BAAI/bge-m3` | Multilingual, 1024-dim, no HF gating. |
| `--port` | `8080` | Distinct from any other inference service on the host. |
| `--hostname` | `0.0.0.0` | Must be reachable from `local-ai-proxy` (which runs on the storage leader, a different host). |
| `--huggingface-hub-cache` | `/Users/dev/tei-models` | Local disk on the Mac; avoids re-downloads on restart. |

Defaults that we accept as-is:

- `max_client_batch_size: 32` — clients chunk accordingly.
- `max_concurrent_requests: 512` — TEI's Tokio async scheduler handles
  far more than the Mac can actually serve; effectively unbounded for us.
- `max_batch_tokens: 16384` — plenty for bge-m3 (8K-token context).

## Current deployment

- **Host**: m2 (Apple M3 Ultra, 512 GB unified memory, 80 GPU cores, Metal 4).
- **Provisioner**: `config/ansible/macos/setup-tei.yml` (wired in via `site.yml`).
- **Daemon**: `com.ycluster.tei` LaunchDaemon under user `dev`.
- **HF cache**: `/Users/dev/tei-models` on the mac's local disk.
- **Backend registration**: `ycluster inference add http://m2.xc:8080 BAAI/bge-m3`.
- **Reachability**: `https://inference.xc/v1/embeddings` (cluster-internal:
  `http://inference.xc/v1/embeddings`) — same surface as the other models.

## Future work

- **`bge-reranker-v2-m3`** — TEI's `--task rerank` mode. Adds ~2 GB
  VRAM; gives cross-encoder reranking on top of the bi-encoder retrieval.
  Worth it once we have a retrieval corpus worth reranking.
- **NVIDIA node** (`nv1–nv5`, 16 GB VRAM plenty) — drop-in for higher
  batch ingestion throughput. Same `ycluster inference add` pattern.
- **Multimodal embeddings** — ColPali / ColQwen for image+text PDFs.
  TEI doesn't serve these; would need a separate `colpali-server` (or
  similar) on its own port, registered under a different model name.
