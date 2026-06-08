# Anthropic Messages API (Kimi)

YCluster exposes an **Anthropic-compatible Messages API** at
`/v1/messages`, served by **Kimi** models running on exo across Apple
Silicon. Anthropic SDKs and tools that speak the Messages API — Claude
Code included — reach it by pointing their base URL at the cluster.

- **Cluster-internal**: `http://inference.xc/v1/messages`
- **External**: `https://your-domain.com/v1/messages`

## How it works (and what it does *not* do)

The inference gateway (`local-ai-proxy`) is a transparent router, not
an API adaptor. It routes by the `model` field in the request body and
forwards the request path (`/v1/messages`) to the backend unchanged. It
does **not** translate between the OpenAI and Anthropic request/response
shapes.

That means `/v1/messages` works **only for models whose backend natively
implements the Anthropic Messages API** — i.e. the Kimi models served by
exo. OpenAI-only backends (vLLM, llama-server, WhisperX) answer on
`/v1/chat/completions` and friends, not on `/v1/messages`. For those,
use the [OpenAI-compatible surface](inference.md) instead.

## Authentication

Auth is the same per-user bearer token used everywhere on the gateway:
your Open-WebUI API key (Account Settings → API Key → Generate) or the
cluster admin master key. It is enforced at the nginx layer.

> **Gotcha:** the cluster authenticates on the **`Authorization: Bearer`**
> header, *not* the Anthropic-standard `x-api-key` header. Clients that
> send only `x-api-key` (the default for the stock `anthropic` SDKs) will
> get a `401` from the gateway. See the SDK/Claude Code notes below for
> how to send the token as a Bearer credential.

## Quick start

```bash
export ANTHROPIC_API_KEY=sk-<your-openwebui-key>
export ANTHROPIC_BASE_URL=http://inference.xc        # or https://your-domain.com

curl -s "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
    ]
  }'
```

`content` may be a plain string (`"content": "Hello"`) or the structured
block form shown above. Set `"stream": true` for incremental SSE output —
the gateway passes Server-Sent Events through with minimal buffering.

## Available models

Kimi model names are the exact backend IDs (no aliasing). The currently
deployed model is:

- `mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8`

List what's actually registered at any time:

```bash
curl -s "$ANTHROPIC_BASE_URL/v1/models" \
  -H "Authorization: Bearer $ANTHROPIC_API_KEY" | jq -r '.data[].id'
```

See [`docs/operations/exo.md`](../operations/exo.md) for the exo serving
topology (tensor-parallel across two M3 Ultras over Thunderbolt RDMA)
and [`docs/operations/inference.md`](../operations/inference.md) for
adding or removing models.

## Using the Anthropic SDK

The stock SDKs default to the `x-api-key` header, which the cluster does
not accept. Pass the token as a Bearer credential via the
`authToken` / `auth_token` option (or a default header) instead.

Python:

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://inference.xc",         # or https://your-domain.com
    auth_token="sk-<your-openwebui-key>",   # sent as Authorization: Bearer …
)

msg = client.messages.create(
    model="mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
print(msg.content[0].text)
```

TypeScript:

```ts
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: "http://inference.xc",          // or https://your-domain.com
  authToken: "sk-<your-openwebui-key>",    // sent as Authorization: Bearer …
});

const msg = await client.messages.create({
  model: "mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8",
  max_tokens: 1024,
  messages: [{ role: "user", content: "Hello" }],
});
console.log(msg.content);
```

## Using Claude Code with Kimi

Claude Code talks the Messages API, so it can run against cluster-hosted
Kimi. Use `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`), **not**
`ANTHROPIC_API_KEY` (sent as `x-api-key`):

```bash
export ANTHROPIC_BASE_URL=https://your-domain.com   # or http://inference.xc
export ANTHROPIC_AUTH_TOKEN=sk-<your-openwebui-key>
export ANTHROPIC_MODEL=mlx-community/Kimi-K2.6-mlx-DQ3_K_M-q8

claude
```

## Limits worth knowing about

- **Backend support is model-specific** — `/v1/messages` only reaches
  Kimi-on-exo. Any other model returns a backend error on this path.
- **Concurrency** — exo serves one instance; concurrent requests batch
  on the GPUs and queue under load. See the K2.6 batch-scaling numbers
  in [`docs/operations/exo.md`](../operations/exo.md).
- **Cold start** — the first request after a redeploy or instance
  placement is slower while the model loads; subsequent requests are
  inference-only.