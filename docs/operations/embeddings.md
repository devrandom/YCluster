# Embeddings Endpoint (BAAI/bge-m3) — Ops

`BAAI/bge-m3` is served by
[`text-embeddings-inference`](https://github.com/huggingface/text-embeddings-inference)
(TEI) on the macOS inference nodes, fronted by the same
`local-ai-proxy` + `inference.xc` nginx vhost as the LLM models. The
endpoint is registered in etcd like every other model — clients just
hit `/v1/embeddings` with `model: "BAAI/bge-m3"` and the proxy routes
it.

See [`docs/usage/inference.md`](../usage/inference.md) for the general
gateway auth + client surface; this page covers the TEI deployment
specifically.

## Architecture

```
client → inference.xc/v1/embeddings   (nginx, auth_request)
       → local-ai-proxy :4001         (model routing, by JSON "model")
       → <tei-host>.xc:8080           (text-embeddings-router, no auth)
         └─ Candle Metal backend on Apple Silicon
            └─ BAAI/bge-m3 weights in ~dev/tei-models/
```

TEI itself runs **with no `--api-key`** — auth is owned by the gateway,
so every request that reaches the TEI process has already cleared the
nginx `auth_request` + the Open-WebUI `api_key` check (or the cluster
master key from `/cluster/config/inference/master-key`). A stray LAN
caller hitting `:8080` directly can complete a request but cannot use
anything from `inference.xc` without a valid cluster token.

## Current deployment

- **Host**: `m2` (Apple M3 Ultra, 512 GB unified memory, 80 GPU cores,
  Metal 4). Opted in via `host_vars/m2.yml` (`tei_enabled: true`).
- **Provisioner**: `config/ansible/macos/setup-tei.yml` (wired in
  `site.yml` after `setup-exo.yml`).
- **Daemon**: `com.ycluster.tei` LaunchDaemon, runs as `dev`. Logs at
  `/opt/homebrew/var/log/tei.log`.
- **HF cache**: `/Users/dev/tei-models` on the mac's local disk
  (`models--BAAI--bge-m3/`).
- **Backend registration**:
  ```bash
  ycluster inference add http://m2.xc:8080 BAAI/bge-m3
  ```

## Opting a new host in

Two flags gate the deployment — neither of them on every macOS node
(only hosts designated as embeddings hosts opt in).

1. **Host vars**: `host_vars/<host>.yml` (in the private repo —
   `config/ansible/host_vars/` is hard-linked to
   `ycluster-private/host_vars/`, see `host_vars.README.md`):
   ```yaml
   tei_enabled: true
   ```
   Without this, the play ends immediately on the host (`meta:
   end_host`).
2. **Backend registration**: register the TEI URL against the model
   so the proxy picks it up. Use the host's `.xc` name (resolves
   cluster-side via dnsmasq):
   ```bash
   ycluster inference add http://<host>.xc:8080 BAAI/bge-m3
   ```

The first host is m2; m1/m3 stay unconfigured unless their host_vars
flip.

## Deploying / Updating

```bash
# From a core node:
ssh "$YC_SYNC_HOST" "cd /etc/ansible && ./run-playbook.sh macos/setup-tei.yml --limit <host>"
```

The playbook is idempotent. Re-runs:

- Re-install the brew formula only if it's missing.
- Leave the HF cache alone (model weights are ~1.15 GB in fp16;
  re-downloading would be wasteful).
- Restart the LaunchDaemon only if `run-tei.sh` or the plist changed.

First start takes ~60s on cold cache (model download + Metal init).
Subsequent starts are ~5s (model loaded from disk).

## Hardware

The TEI formula ships a Metal backend via Candle. Per TEI's own
benchmarks, Metal is roughly 2–5× slower than an A100 for `bge-m3`
batch inference — fine for personal-scale corpora, but if batch
ingestion volumes grow past personal scale, an NVIDIA host (`nv1+`,
16 GB VRAM is plenty for bge-m3) is the better target. TEI's
`cuda-1.9` Docker image is the supported path on NVIDIA.

M3 Ultra / M4 Ultra with ≥64 GB unified memory is the sweet spot on
Apple Silicon — the unified memory pool means a much larger model
later (Qwen3-Embedding-8B, etc.) fits without a separate VRAM
ceiling.

## API shape

OpenAI-compatible. Request:

```json
{"model": "BAAI/bge-m3",
 "input": ["text one", "text two"],
 "encoding_format": "float"}
```

Response:

```json
{"data": [{"index": 0, "embedding": [0.0123, ...]},
          {"index": 1, "embedding": [0.0789, ...]}],
 "model": "BAAI/bge-m3",
 "usage": {"prompt_tokens": N, "total_tokens": N}}
```

- `input` may be a string or array of strings.
- Each embedding has length **1024** (bge-m3's native dim).
- **Batch limit**: TEI's default `max_client_batch_size` is **32**.
  Requests with more than 32 inputs return `422 batch size > 32`.
  Clients must chunk accordingly.

Example (cluster-internal):

```bash
curl -s -X POST "$OPENAI_BASE_URL/embeddings" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "BAAI/bge-m3", "input": ["hello world"]}'
```

Other endpoints (not needed for the OpenAI path): `GET /health` (200
empty body = ready), `POST /embed` (native TEI shape, slightly lower
overhead than the OpenAI path), `GET /info` (model metadata, 200 once
auth is in front of it).

## Notes / known behaviors

- **safetensors fallback**: bge-m3 isn't published as `safetensors`
  on HF, so TEI falls back to `pytorch_model.bin`. Logged as a WARN
  on every start. Functionally fine; load is just slower (~56s cold
  vs ~5s for safetensors).
- **GeLU + tanh approximation**: TEI notes its Bert backend uses the
  tanh-approximated GeLU rather than exact GeLU when
  `hidden_act=gelu`. Negligible inference-quality impact per TEI.
- **No `--api-key` on TEI**: design choice — the gateway is the only
  auth surface (see Architecture above). If you ever need to close
  even the direct `:8080` path, firewall the cluster ports from
  non-cluster subnets or bind TEI to a specific interface.
