# LLM Inference (OpenAI-compatible)

YCluster exposes an OpenAI-compatible chat/completions API. OpenAI
client libraries reach it with no special configuration — just point
them at the cluster's inference base URL.

- **Cluster-internal**: `http://inference.xc/v1/`
- **External**: `https://your-domain.com/v1/`

Behind the scenes the inference gateway (`local-ai-proxy`) routes each
request by its `model` field to the right backend (vLLM, llama-server,
llama.cpp, mlx_lm, exo, …). It's a transparent router, not an API
adaptor — it forwards the request path unchanged and does not rewrite
request/response shapes. See
[`docs/operations/inference.md`](../operations/inference.md) for the
deployment and ops side.

> Anthropic Messages API (`/v1/messages`) and audio transcription
> (`/v1/audio/…`) ride the same gateway with the same auth. See
> [`anthropic-messages.md`](anthropic-messages.md) and
> [`transcription.md`](transcription.md) for those surfaces.

## Authentication

Auth is a per-user bearer token, enforced at the nginx layer. Generate
one in Open-WebUI (Account Settings → API Key → Generate); the same
`sk-…` key authenticates both Open-WebUI and direct API calls. The
cluster admin master key also works.

## What's available

Models map to backends in etcd and change over time. List what's
actually registered right now:

```bash
curl -s "$OPENAI_BASE_URL/models" \
  -H "Authorization: Bearer $OPENAI_API_KEY" | jq -r '.data[].id'
```

On a core node, `ycluster inference ls` shows the same set with their
backends.

## Quick start

```bash
export OPENAI_API_KEY=sk-<your-openwebui-key>
export OPENAI_BASE_URL=http://inference.xc/v1   # or https://your-domain.com/v1

curl -s -X POST "$OPENAI_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<a-model-id-from-/models>",
    "messages": [{"role": "user", "content": "Say hello in one word"}]
  }'
```

Python (official OpenAI SDK):

```python
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL from env

resp = client.chat.completions.create(
    model="<a-model-id-from-/models>",
    messages=[{"role": "user", "content": "Say hello in one word"}],
)
print(resp.choices[0].message.content)
```

## Streaming

Set `"stream": true` for incremental SSE output — the gateway passes
Server-Sent Events through with minimal buffering.

```python
stream = client.chat.completions.create(
    model="<a-model-id-from-/models>",
    messages=[{"role": "user", "content": "Write a haiku about etcd"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

## Reasoning models

Reasoning ("thinking") backends stream their chain-of-thought in a
separate field from the final answer. Two shapes are in the wild and
both come straight through the gateway:

- `delta.reasoning` — OpenAI o-series, gpt-oss
- `delta.reasoning_content` — vLLM, MiniMax

Read whichever is populated; treat them as the same channel.

## Tokenizer passthrough

The proxy forwards `/v1/tokenize` and `/v1/detokenize` to the backend
(llama.cpp native; vLLM accepts the same paths). Useful for counting
tokens against a specific model:

```bash
curl -s -X POST "$OPENAI_BASE_URL/tokenize" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "<model-id>", "content": "hello world"}'
```

Not every backend implements these — expect an error from backends
that don't.

## Sample script

[`contrib/test-inference.py`](../../contrib/test-inference.py) is a
streaming smoke test: it does a tokenizer round-trip, streams a short
completion (reasoning in dim gray, answer at normal weight), and prints
ttft / tok-s / total at the end. Endpoint + bearer token come from
`config.yml` at the repo root (see
[`contrib/_cluster_config.py`](../../contrib/_cluster_config.py)).

```bash
# config.yml at the repo root:
#   endpoint: https://your-cluster.example/
#   api_token_file: my.token

python3 contrib/test-inference.py <model-id>
# or: MODEL=<model-id> python3 contrib/test-inference.py
```
</content>
</invoke>
