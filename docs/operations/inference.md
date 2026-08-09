# Inference Gateway

YCluster runs `local-ai-proxy` as the inference gateway: one
OpenAI-compatible endpoint in front of every backend (vLLM,
llama-server, llama.cpp, mlx_lm, etc.).

- **Cluster-internal**: `http://inference.xc/v1/`
- **External**: `https://your-domain.com/v1/`

Model → backend mappings live in etcd under
`/cluster/config/inference/models/` and are watched by the proxy
(hot-reload — no restart on add/remove). Health and load state live
in memory and show up on `/healthz`.

## Managing Models

```bash
# List configured models with their backends
ycluster inference ls

# Add a backend, auto-discovering every model it serves
ycluster inference add http://nv1.xc:8000

# Add one specific model from a backend
ycluster inference add http://nv1.xc:8000 MiniMaxAI/MiniMax-M2.7

# Remove a model entirely
ycluster inference remove qwen3-30b

# Remove just one backend from a fanned-out model
ycluster inference remove qwen3-30b --api-base http://m2.xc:8080

# Remove a backend from EVERY model that has it — the inverse of bulk
# `add <url>`. Mirrors `add`'s shape: a positional containing :// is
# treated as a backend URL.
ycluster inference remove http://m1.xc:52415

# Remove one backend from one model, backend-first (same as the
# --api-base form above, just positional)
ycluster inference remove http://m2.xc:8080 qwen3-30b
```

Backend URLs are host-only (no `/v1` suffix) — the proxy appends the
client path as-is.

`remove` mirrors `add`'s argument shape: `add`/`remove <url> [model]`
operate on a backend (all its models, or one), while `remove <model>`
still works model-first. A positional containing `://` is the backend.

A backend that auto-discovers more models than you want (e.g. exo
exposes its entire ~120-model catalog at `/v1/models`) is best added
by naming the specific model: `ycluster inference add <url> <model>`.
If you already ran the bulk form, `ycluster inference remove <url>`
undoes it in one go.

Under the hood `ycluster inference …` shells out to the proxy's own
CLI (`local-ai-proxy models …`), which owns the etcd schema. You can
call it directly on a core node if you prefer.

## Health and Status

```bash
# Backend + model health with state per backend
ycluster inference status

# Pull a backend out of rotation (ops, no alert noise)
ycluster inference disable http://x1.xc:8080 --reason "decommissioned"

# Restore it
ycluster inference enable http://x1.xc:8080

# Restart the proxy (only for YAML config changes — model edits are hot)
ycluster inference reload
```

Disabled backends are tracked at
`/cluster/config/inference/disabled/<url>` and excluded from the
health checker's rotation.

## Per-User API Keys

Gateway API keys are etcd-backed and self-service. Users mint keys at
`https://<admin-domain>/admin/settings/api-keys` (the key is shown once
at mint and never re-displayed) and manage their own keys there. Ops
manage keys cluster-wide with the CLI:

```bash
ycluster inference key create --owner alice@example.com --group g1 --label laptop
ycluster inference key list [--owner alice@example.com]
ycluster inference key revoke <key-id>
```

Each key is a distinct `sk-…` value stored as an argon2 hash under
`/cluster/users/<email>/keys/` in etcd. A single user can hold multiple
keys (e.g. one per device), each revocable independently.

The same key previously also signed a user into Open-WebUI; OWUI keeps
its own per-user keys for signing in to OWUI, but they are no longer
how the gateway authenticates requests. An existing OWUI key's value
was copied into the new etcd schema during migration, so the token
keeps working — there is no rotation.

Client setup:

```bash
export OPENAI_API_KEY=sk-<your-gateway-key>
export OPENAI_BASE_URL=http://inference.xc/v1         # cluster-internal
# or
export OPENAI_BASE_URL=https://your-domain.com/v1     # external
```

See [`docs/usage/inference.md`](../usage/inference.md) for the
client-facing API (chat/completions, streaming, model discovery).

Authentication is enforced at the nginx layer via `auth_request`; the
proxy itself trusts the `X-User-Id` header that nginx injects after a
valid token check.

## WARNING: Do Not Add External Providers Directly to Open-WebUI

**All inference backends must be added through
`ycluster inference add`**, never via Open-WebUI's Admin Panel →
Settings → Connections page.

Open-WebUI is configured with `ENABLE_FORWARD_USER_INFO_HEADERS=True`
so it attaches `X-OpenWebUI-User-Name`, `X-OpenWebUI-User-Email`, and
`X-OpenWebUI-User-Id` on **every request to every configured
backend**.

When the only backend is the inference gateway (loopback), those
headers never leave the cluster. If someone wires an external
provider (OpenAI, Anthropic, etc.) directly into the Connections
page, **user emails and names get shipped to that provider in HTTP
headers**.

`local-ai-proxy` strips upstream bearer tokens and does not forward
these headers to backends, so going through the gateway is the safe
path.

## Architecture

- **Systemd service**: `local-ai-proxy.service` (part of
  `ycluster-apps.target`, runs on every storage node).
- **Auth validator**: `local-ai-proxy-auth.service` — small Flask app
  called by nginx `auth_request`. Accepts either the cluster admin
  master key (stored at `/cluster/config/inference/master-key` in
  etcd) or an etcd per-user key under `/cluster/users/<email>/keys/`
  (argon2-verified).
- **Model store**: etcd prefix `/cluster/config/inference/models/`;
  each key is a model name, the JSON value is
  `{"backends":[{"api_base":"…"}, …]}`. The schema is fan-out-ready
  (the router currently uses `backends[0]` and logs a warning when
  multiple are listed).
- **Disabled set**: etcd prefix `/cluster/config/inference/disabled/`;
  each key is a backend URL, the value is optional JSON metadata.
- **Runtime config**: `/etc/local-ai-proxy/config.yaml` (listen
  address, etcd endpoints/prefix, trusted-proxy CIDRs, health
  interval). The API listener binds loopback (`127.0.0.1:4001`) and
  `/metrics` is served on a separate `inference_proxy_metrics_port`
  (`4010`) exposed to Prometheus; only nginx / local tools reach the
  API. Model edits are hot; restart only for YAML changes.
### Request Paths

```
Direct API:  client → inference.xc (nginx + auth_request) → local-ai-proxy → backend
Open-WebUI:  browser → Open-WebUI → local-ai-proxy → backend
External:    client → your-domain.com/v1/ (nginx + auth_request) → local-ai-proxy → backend
```

See [`docs/local-ai-proxy.md`](../local-ai-proxy.md) for the proxy's
design rationale (it replaced LiteLLM in this cluster) and how it
differs from LiteLLM / Bifrost / one-api on client-disconnect
handling.

For audio transcription (OpenAI `/v1/audio/transcriptions` and
`/v1/audio/translations`) the proxy uses the same `model` routing
key, just read from the multipart form field instead of the JSON
body. See [`docs/usage/transcription.md`](../usage/transcription.md)
for the API surface, and [`transcription.md`](transcription.md) for
the WhisperX deployment on AMD Strix Halo (gfx1151).

Embeddings (`/v1/embeddings`, model `BAAI/bge-m3`) ride the same
gateway with the same `model` routing key. See
[`embeddings.md`](embeddings.md) for the TEI deployment on the macOS
inference nodes (Apple Silicon Metal backend).

Document extraction (`/v1/file_parse`, models `document-parse` and
`mineru-parse`) rides the same gateway too, with the proxy routing by
the `model` form field in the multipart body and rewriting the path to
MinerU's native `/file_parse` (parallels the `/v1/tokenize → /tokenize`
rewrite for llama.cpp/vLLM). See
[`document-extraction.md`](document-extraction.md) for the MinerU
deployment on macOS (MLX on M3 Ultra).

Clients that can't easily embed `model` in the body (raw curl uploads,
binary-only clients) can pin it in the `X-Model` header instead — the
header overrides any `model` field in the JSON body or multipart form,
for every endpoint the proxy routes.

The proxy routes by `model` and forwards the request path verbatim, so
backends that natively speak the Anthropic Messages API are reachable at
`/v1/messages` with no translation layer. The Kimi models on exo do; see
[`docs/usage/anthropic-messages.md`](../usage/anthropic-messages.md) for
the client-facing API (including the `Authorization: Bearer` vs
`x-api-key` caveat) and [`exo.md`](exo.md) for the serving topology.
