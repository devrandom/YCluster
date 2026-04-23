# local-ai-proxy

An OpenAI-compatible inference proxy for self-hosted LLM backends
(vLLM, llama-server, mlx-server, SGLang, etc.). Routes requests by
model name to a configured set of backends, tracks per-backend
health, and passes responses through unchanged so vendor-specific
fields keep working on day one.

Written in Go, `net/http`, no framework.

## Features

- **Model-name routing.** Each request's `model` field selects the
  backend via an in-memory map rebuilt from YAML config or an etcd
  prefix (hot-reloaded on changes).
- **Transparent passthrough.** Reasoning fields, token IDs, logprobs,
  vendor-specific params — all flow through unchanged, so new backend
  features work without waiting for a schema update.
- **Fan-out with least-loaded routing.** Multiple backends per model
  are load-balanced by current in-flight count (tie-break: random).
  A failure against one backend (transport error or any 4xx/5xx) is
  transparently retried against a peer; only the last attempt's
  response is returned. Each retry also fires an out-of-band health
  probe so transient glitches don't strand traffic for a full tick.
- **Per-backend health checks.** Periodic `GET /v1/models` probe with
  state (healthy/down/disabled); transitions log at INFO/WARN. The
  synthesized `/v1/models` hides disabled and unavailable models so
  clients don't pick dead routes.
- **Operator-disabled backends.** Mark a URL as known-down so it
  stops polling, stops alerting, and its models become implicitly
  disabled — useful during planned downtime.
- **Client-disconnect cancellation.** When a client closes a streaming
  request, the upstream backend request is cancelled, freeing the
  slot. Covers both non-streaming and SSE paths. (Every GPU cycle
  after Ctrl-C is wasted compute.)
- **Auth stays out of the proxy.** Put nginx `auth_request` (or any
  reverse proxy that does the same) in front and trust `X-User-Id`
  from loopback. See `docs/deploy.md`.

## Quick start

```bash
make build

# Point it at a single upstream (passthrough mode).
./bin/local-ai-proxy --backend http://localhost:8080

# Proxy listens on :4000 by default.
curl -s http://localhost:4000/v1/models
curl -s http://localhost:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hi"}]}'
```

## Configuration

Pass `--config config.yaml`. Exactly one of `backend`, `backends`, or
`etcd` must be set.

```yaml
listen: ":4000"

# Option 1: single-backend passthrough (no model routing).
backend:
  url: "http://localhost:8080"

# Option 2: static YAML list. Multiple entries with the same model:
# accumulate — the router fan-outs across them, picking the least-loaded
# healthy backend and retrying failures against peers.
backends:
  - model: "llama-3.1-70b"
    api_base: "http://gpu1:8000"
  - model: "qwen-7b"
    api_base: "http://mac1:8080"

# Option 3: etcd prefix. Each key is a model name; the JSON value is
# {"backends":[{"api_base":"..."}]}. Put/Delete events propagate
# without restart. endpoints defaults to ["http://localhost:2379"].
etcd:
  prefix: "/inference/models/"

# Optional. CIDRs from which X-User-Id is trusted. Default: loopback.
trusted_proxies:
  - 127.0.0.1/32
  - ::1/128

# Optional. Health check cadence (default 30s; 0 disables).
health_check_interval: 30s
```

## Endpoints

| Path                                             | Method | Description                           |
|--------------------------------------------------|--------|---------------------------------------|
| `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/…` | POST | Routed by the `model` field in the body |
| `/v1/models`                                     | GET    | Synthesized list of servable models   |
| `/healthz`                                       | GET    | Per-backend + per-model health rollup |

Requests outside `/v1/` in model-routed mode return 404.

## Deployment

See [`docs/deploy.md`](docs/deploy.md) for the systemd unit, nginx
`auth_request` recipe, and notes on trusted-proxy / etcd-disabled
backend management.

## Development

Go 1.24 or later.

```bash
make test        # go test ./...
make test-race   # go test -race ./...
make vet
make build       # binary at bin/local-ai-proxy
```

Tests use `httptest.NewServer` with deterministic channel
synchronization — no sleep-based waits.

## License

Apache 2.0 — see [LICENSE](LICENSE).
