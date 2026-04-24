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
```

Backend URLs are host-only (no `/v1` suffix) — the proxy appends the
client path as-is.

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

Users generate keys in Open-WebUI (Account Settings → API Key →
Generate). The same `sk-…` key authenticates both Open-WebUI and
direct API calls to the inference gateway.

```bash
export OPENAI_API_KEY=sk-<your-openwebui-key>
export OPENAI_BASE_URL=http://inference.xc/v1         # cluster-internal
# or
export OPENAI_BASE_URL=https://your-domain.com/v1     # external
```

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
  called by nginx `auth_request`. Accepts either the internal service
  key (stored at `/cluster/config/litellm/master-key` in etcd for
  historical reasons — the path name is legacy, the key itself is
  reused) or a row in Open-WebUI's `api_key` PostgreSQL table.
- **Model store**: etcd prefix `/cluster/config/inference/models/`;
  each key is a model name, the JSON value is
  `{"backends":[{"api_base":"…"}, …]}`. The schema is fan-out-ready
  (the router currently uses `backends[0]` and logs a warning when
  multiple are listed).
- **Disabled set**: etcd prefix `/cluster/config/inference/disabled/`;
  each key is a backend URL, the value is optional JSON metadata.
- **Runtime config**: `/etc/local-ai-proxy/config.yaml` (listen
  address, etcd endpoints/prefix, trusted-proxy CIDRs, health
  interval). Model edits are hot; restart only for YAML changes.
- **Rollback option**: `litellm.service` is still installed but
  stopped and disabled by default. Re-enable it with
  `ansible-playbook app/install-litellm.yml -e litellm_enabled=true`
  if a rollback is needed.

### Request Paths

```
Direct API:  client → inference.xc (nginx + auth_request) → local-ai-proxy → backend
Open-WebUI:  browser → Open-WebUI → local-ai-proxy → backend
External:    client → your-domain.com/v1/ (nginx + auth_request) → local-ai-proxy → backend
```

See [`docs/local-ai-proxy.md`](../local-ai-proxy.md) for the proxy's
design rationale and how it differs from LiteLLM / Bifrost / one-api
on client-disconnect handling.
