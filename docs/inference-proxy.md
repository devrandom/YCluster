# Inference Proxy: Design Doc

## Problem

LLM proxy/gateways (LiteLLM, Bifrost, one-api) are built for cloud API
aggregation. They treat backends as elastic, stateless endpoints and
allocate 80%+ of their code to cloud provider adaptors. Self-hosted
inference has fundamentally different problems:

- **Client disconnect does not cancel upstream requests.** LiteLLM holds
  orphaned connections open indefinitely. Bifrost acknowledges the same
  limitation (fasthttp lacks context cancellation). one-api uses
  `http.NewRequest` instead of `http.NewRequestWithContext`. Every major
  proxy gets this wrong.

- **Backends have heterogeneous concurrency.** A 4×H100 vLLM instance
  handles 16+ concurrent requests via continuous batching. A Mac Studio
  running llama-server processes one at a time. The proxy must be
  load-aware, not just alive-aware.

- **Model loading is slow and stateful.** llama-server in router mode
  lazy-loads large models on first request (minutes of mmap warmup for
  100+ GB models). A naive health check that triggers a load is
  destructive. Backends transition between unloaded/loading/ready states.

- **Backends crash, OOM, and hang.** Self-hosted inference doesn't have
  cloud provider SLAs. The proxy must detect stuck backends (no output
  for N seconds) and route around them.

## Goals

A lightweight, open-source inference proxy for self-hosted backends.
OpenAI-compatible API in, OpenAI-compatible API out. Written in Go,
using `net/http` for native `context.Context` propagation.

### Must have

- **Request cancellation on client disconnect** — both streaming and
  non-streaming. This is the primary motivator. Go's `net/http` cancels
  `Request.Context()` on client disconnect; upstream requests made with
  `http.NewRequestWithContext` inherit this automatically.

- **OpenAI-compatible `/v1/` endpoint** — chat/completions, completions,
  models. Transparent passthrough of backend-specific params (logprobs,
  top_logprobs, reasoning_content, etc.).

- **Multi-backend routing** — multiple backends can serve the same model.
  Route by model name. Weighted routing (prefer faster backends).

- **Concurrency-aware routing** — track in-flight requests per backend.
  Respect per-backend concurrency limits (e.g. max 1 for llama-server,
  max 16 for vLLM). Queue or reject when at capacity rather than
  overloading.

- **Backend health tracking** — periodic liveness checks. Track
  backend state: healthy / degraded (slow responses) / down (unreachable
  or error). Auto-remove from rotation when down, auto-restore when
  healthy.

- **Identity from a trusted upstream gateway** — the proxy does not
  validate bearer tokens itself. It expects a reverse proxy (nginx
  `auth_request`, Envoy `ext_authz`, Traefik ForwardAuth, etc.) to have
  already authenticated the request and injected `X-User-Id`. The proxy
  uses this identity for per-user in-flight tracking, usage logging, and
  future quotas. Binding to localhost or a trusted subnet keeps header
  spoofing out of scope. This lets arbitrary auth backends (Open-WebUI's
  `api_key` PG table, LDAP, OIDC, static file) be wired in at the gateway
  without the proxy linking any of them.

- **Static-token fallback** — for standalone or dev use without a fronting
  gateway, a YAML `token → user_id` map validates bearer tokens in the
  proxy. No DB, no callouts. Anyone who needs richer auth puts a gateway
  in front.

- **Streaming SSE passthrough** — proxy Server-Sent Events with minimal
  buffering. Detect write failures to trigger upstream cancellation.

- **Runtime model/backend management** — add/remove backends and model
  mappings via API without restart.

### Nice to have

- **Model aliasing** — map friendly names to backend-specific model IDs
  (e.g. "kimi" -> "Kimi-K2.5-Q3_K_S" on llama-server).

- **Usage tracking** — log prompt/completion tokens per request, per
  user, per model. Expose via API for dashboards.

- **Spend estimation** — token-based cost tracking per user/key with
  optional quotas.

- **Backend readiness detection** — distinguish "up but loading model"
  from "ready to serve." Avoid routing to a backend that will block on
  cold model load.

- **Stuck request detection** — if a streaming response produces no
  chunks for N seconds, consider the backend stuck and cancel.

- **Prometheus metrics** — requests in flight, latency histograms,
  backend health state, tokens/sec per backend.

### Non-goals

- Cloud provider adaptors (OpenAI, Anthropic, Azure, Bedrock, etc.).
  All backends speak OpenAI-compatible API.
- Semantic caching, guardrails, PII filtering.
- MCP gateway, tool orchestration.
- Web UI (API-first; external dashboards can consume the API).

## Architecture

```
                                ┌──────────────┐
                                │  vLLM (GPU)  │
  client ──▶ proxy ──▶ router ──┤  llama.cpp   │
               │                │  mlx_lm      │
               │                │  vllm-mlx    │
               ▼                └──────────────┘
          auth + log
```

### Components

**HTTP server** (`net/http`): Accepts OpenAI-compatible requests.
Each request carries a `context.Context` that is cancelled when the
client disconnects. This is the foundation — everything downstream
inherits this context.

**Auth middleware**: In trusted-gateway mode (default), reads `X-User-Id`
from the incoming request and attaches it to the context. In
static-token mode, extracts the Bearer token, looks it up in a
YAML-configured map, and attaches the resolved identity. Either way,
downstream code sees the same `ctx.Value(userKey)`.

**Router**: Looks up model name → list of backends. Selects a backend
based on: health state, current in-flight count, weight, concurrency
limit. If no backend is available, returns 503.

**Backend proxy**: Makes upstream request using
`http.NewRequestWithContext(ctx, ...)`. For streaming, reads SSE chunks
from upstream and writes to client, checking for write errors on each
chunk. Context cancellation from either client disconnect or stuck
detection propagates to the upstream connection automatically.

**Health checker**: Periodic goroutine per backend. Calls
`GET /v1/models` (or configurable endpoint). Updates backend state.
Respects backend-specific intervals (don't poll a Mac Mini every 5s
if it takes 30s to respond while loaded).

**Model registry**: In-memory map of model name → []backend. Mutated
via admin API. Optionally persisted to a config file or database.

### Backend configuration

```yaml
backends:
  - name: gpu-server
    url: http://gpu1:8000/v1
    max_concurrent: 16
    weight: 10
    health_interval: 30s
    models:
      - "my-org/large-model"

  - name: mac-llama
    url: http://mac1:8080/v1
    max_concurrent: 1
    weight: 1
    health_interval: 60s
    models:
      - name: "chat"
        backend_model: "Model-Q3_K_S"

  - name: mac-mlx
    url: http://mac1:8088/v1
    max_concurrent: 1
    weight: 1
    models:
      - "mlx-community/SmallModel-4bit"
      - "mlx-community/MediumModel-4bit"
```

### Request flow

1. Client sends `POST /v1/chat/completions` with `model: "chat"`.
2. Auth middleware reads `X-User-Id` from the request (trusted-gateway
   mode) or looks up the Bearer token in the static map. Identity is
   attached to the request context.
3. Router resolves "chat" → backends `[mac-llama, mac2-llama]`.
4. Router picks the backend with lowest in-flight count that is healthy
   and under its concurrency limit.
5. If model alias exists (chat → Model-Q3_K_S), rewrite model field
   in request body.
6. Proxy creates upstream request with `http.NewRequestWithContext(ctx)`.
7. For streaming: read SSE chunks from upstream, write to client. On
   write error → context cancelled → upstream connection closed →
   backend aborts the request.
8. For non-streaming: `http.Do(req)` blocks. If client disconnects,
   context cancelled → `http.Do` returns with error → upstream
   connection closed.
9. Log usage, update in-flight count, return response.

## Implementation plan

### Phase 1: Core proxy (replace LiteLLM for our cluster)

- net/http server with `/v1/chat/completions`, `/v1/completions`,
  `/v1/models`
- Static YAML config for backends and models
- Context-aware upstream proxy (the whole point)
- Streaming SSE passthrough with write-error detection
- In-flight tracking per backend
- Concurrency limits per backend
- Trusted-gateway auth (read `X-User-Id`) with static-token YAML
  fallback
- Health checks (periodic GET /v1/models)
- Admin API: add/remove backend, list backends with status

### Phase 2: Production features

- Model aliasing
- Weighted routing
- Usage logging (tokens per request, keyed by `X-User-Id`)
- Stuck request detection
- Prometheus metrics endpoint
- Graceful shutdown (drain in-flight requests)

### Phase 3: Community features

- Config hot-reload (watch file or SIGHUP)
- Per-user spend tracking and quota enforcement (indexed by
  `X-User-Id`; token storage stays at the gateway)
- Backend auto-discovery (query /v1/models on new backend, register
  all models automatically)
- Request queuing with timeout (instead of 503 when at capacity)

## Prior art

| Project | LOC | Language | Disconnect handling | Focus |
|---------|-----|----------|-------------------|-------|
| LiteLLM | large | Python | broken (known issue 2+ years, fix reverted) | cloud APIs |
| Bifrost | 285k | Go (fasthttp) | streaming only (no context in fasthttp) | cloud APIs |
| one-api | 22k | Go (gin/net/http) | broken (http.NewRequest, one-line fix) | cloud APIs |
| Portkey | med | TypeScript | undocumented | cloud APIs |
| llama-swap | ~8k | Go (net/http) | broken (no ctx threading in ProxyRequest; panic-recover instead) | local hot-swap |
| GPUStack | med | Python | not audited | local orchestration |
| KubeAI | med | Go | expects cluster-level auth; proxy layer not audited | K8s operator |
| vLLM production-stack router | med | Python (FastAPI) | inconsistent (httpx) | homogeneous vLLM on K8s |

Cloud-first proxies all miss the disconnect→cancel chain. Among
local-first proxies, llama-swap is the closest structural fit:
idiomatic Go, per-backend concurrency semaphores that map cleanly onto
"max 1 for llama-server, max 16 for vLLM," and a small, readable
codebase. It falls short in three ways that matter here:

- **No same-model replica fan-out.** `Models` is a
  `map[string]ModelConfig` (`proxy/config/config.go`); peer routing
  explicitly drops duplicate model mappings. Replicating one model
  across nv1+nv2 and picking least-loaded would require reshaping the
  core data model.
- **Static-list auth only.** `apiKeys []string` with `==` comparison.
  No hook, no webhook, no gateway-delegation path.
- **Client-disconnect cancellation is missing** — the original
  motivation.

The cancellation fix is small (~50 lines) and worth upstreaming as a
good-citizen PR regardless of what we build. Replica fan-out is a much
larger surgery and effectively rules out adopting llama-swap as-is for
this cluster.

## Open questions

- **Name?** Needs a short, memorable project name.
- **Config format?** YAML file vs. API-only vs. both.
- **Where to host?** Separate GitHub repo (not in ycluster).
- **Minimum Go version?** 1.22+ for enhanced routing in net/http.
