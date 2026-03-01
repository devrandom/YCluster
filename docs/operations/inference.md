# Inference Gateway

YCluster runs a LiteLLM inference gateway that provides a single OpenAI-compatible API endpoint for all inference backends (vLLM, llama-server, etc.).

- **Cluster-internal**: `http://inference.xc/v1/`
- **External**: `https://your-domain.com/v1/`
- **LiteLLM UI**: `http://inference.xc/ui/` (login: username `admin`, password = master key)

## Managing Models

```bash
# List all configured models
ycluster inference models

# Add a backend (auto-discovers served models)
ycluster inference add nv1.xc

# Add with explicit port
ycluster inference add m1.xc:8080

# Remove a model
ycluster inference remove my-model

# Remove a specific backend for a model
ycluster inference remove my-model --api-base http://m1.xc:8080/v1

# Print the master API key
ycluster inference key

# Restart LiteLLM (only needed for config.yaml changes; model add/remove is instant)
ycluster inference reload
```

URL shorthand: `nv1.xc` expands to `http://nv1.xc:8000/v1`, `m1.xc:8080` expands to `http://m1.xc:8080/v1`.

## Adding External LLM Providers

External providers (OpenAI, Anthropic, etc.) must be added through LiteLLM, not directly to Open-WebUI. See the [privacy warning](#warning-do-not-add-external-providers-directly-to-open-webui) below.

```bash
ycluster inference add my-model https://api.openai.com/v1 --api-key sk-...
```

## Per-User API Keys

Users generate API keys in Open-WebUI (Account Settings -> API Key -> Generate). The same `sk-...` key works for both Open-WebUI and direct API access to the inference gateway.

Configure OpenCode or other clients:
```bash
export OPENAI_API_KEY=sk-<your-openwebui-key>
export OPENAI_BASE_URL=http://inference.xc/v1       # cluster-internal
# or: export OPENAI_BASE_URL=https://your-domain.com/v1  # external
```

## Per-User Spend Tracking

All requests are tracked with per-user attribution in LiteLLM's spend logs (`end_user` column):

- **Direct API calls** (OpenCode, curl): User is identified from their Open-WebUI API key via the custom auth hook
- **Open-WebUI chat**: User is identified via `X-OpenWebUI-User-Email` header forwarding (`ENABLE_FORWARD_USER_INFO_HEADERS=True`)

The LiteLLM UI at `http://inference.xc/ui/` shows usage filtered by end user.

## WARNING: Do Not Add External Providers Directly to Open-WebUI

**All inference backends must be added through LiteLLM** (`ycluster inference add`), not directly to Open-WebUI's Admin Panel -> Settings -> Connections page.

Open-WebUI is configured with `ENABLE_FORWARD_USER_INFO_HEADERS=True` for per-user spend tracking. This means it sends `X-OpenWebUI-User-Name`, `X-OpenWebUI-User-Email`, and `X-OpenWebUI-User-Id` headers on **every request to every configured backend**.

When the only backend is LiteLLM (running locally on the same machine), these headers never leave the cluster. But if someone adds an external provider directly to Open-WebUI's Connections page, **user emails and names would be sent to that provider in HTTP headers**.

LiteLLM does not forward these headers to upstream providers, so adding backends through `ycluster inference add` is safe.

## Architecture

- **Systemd service**: `litellm.service` (part of `ycluster-apps.target`, runs on storage leader)
- **Docker image**: Custom image extending `ghcr.io/berriai/litellm-database` with `asyncpg`
- **Model storage**: LiteLLM PostgreSQL database (`STORE_MODEL_IN_DB=True`)
- **Secrets**: Stored in etcd at `/cluster/config/litellm/{master-key,salt-key,db-password}`
- **Auth flow**: Custom auth hook checks master key -> LiteLLM internal tokens -> Open-WebUI `api_key` table
- **Config files**: `/etc/litellm/config.yaml` (static settings), models managed via API

### Request Paths

```
Direct API:  client -> inference.xc (nginx) -> LiteLLM :4000 -> backend
Open-WebUI:  browser -> Open-WebUI -> LiteLLM :4000 -> backend
External:    client -> your-domain.com/v1/ (nginx) -> LiteLLM :4000 -> backend
```
