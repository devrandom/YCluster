# local-ai-proxy — TODO

See `docs/local-ai-proxy.md` for the design rationale.

## Done

- [x] Single-backend reverse proxy with client-ctx threaded to upstream
- [x] Hop-by-hop header stripping (RFC 7230 §6.1)
- [x] Streaming body with per-read flush (SSE visible in real time)
- [x] Test: client disconnect cancels non-streaming upstream
- [x] Test: client disconnect cancels streaming upstream
- [x] Test: streaming is not buffered
- [x] Test: hop-by-hop headers stripped both directions
- [x] YAML config with flag overrides
- [x] Structured request log via `slog` (method, path, status, duration,
      bytes, `X-User-Id` when present)
- [x] Graceful shutdown on SIGTERM/SIGINT with 30s drain cap and
      force-close fallback
- [x] Error response in OpenAI shape (`{"error": {"message", "type"}}`)
- [x] End-to-end smoke tested against real vLLM (MiniMax-M2.7): models,
      chat completion, streaming, and disconnect → backend slot freed
- [x] Ansible deploy on s3:4001 alongside LiteLLM
- [x] Pluggable backend Source interface
- [x] YAMLSource (static `backends:` list keyed by model name)
- [x] EtcdSource (watches etcd prefix; hot-reload on Put/Delete)
- [x] ModelRouter: parses model from request body, routes accordingly
- [x] Synthesized `GET /v1/models` from the router's known models
- [x] `-race` clean (35 tests)

## Near-term

- [x] One-time migration script: `scripts/migrate-litellm-to-etcd.py`
      reads LiteLLM master key from etcd, calls `/v1/model/info`, writes
      each model to `/cluster/config/inference/models/<name>` with the
      `/v1` stripped from api_base (dry-run supported)
- [x] Ansible: `use_etcd=true` deploy mode renders the etcd stanza
- [x] Fan-out-ready schema: etcd value is
      `{"backends":[{"api_base":"..."},...]}`; router picks [0] until
      fan-out lands, no storage migration needed
- [x] Migrated + flipped s3:4001 to etcd mode (11 models live)
- [ ] Auth middleware: trust `X-User-Id` from upstream, static bearer
      fallback map (postponed explicitly by user)

## Multi-backend — the fuller differentiator

- [ ] Multiple backends per model (fan-out) with least-loaded selection
      (schema already supports it — just router changes)
- [ ] In-flight counter per backend
- [ ] Per-backend `max_concurrent` concurrency cap
- [ ] Periodic health checks (`GET /v1/models` or configurable endpoint)
- [ ] Track backend state: healthy / degraded / down
- [ ] Model aliasing (friendly name → backend model ID, rewrite request
      body)
- [ ] Tests: least-loaded wins; full backend skipped; 503 when all full;
      concurrency cap honored under parallel load

## Production features

- [ ] Usage logging: prompt/completion tokens per request, keyed by
      `X-User-Id`
- [ ] Stuck-request detection: no upstream bytes for N seconds → cancel
- [ ] Prometheus metrics: in-flight gauge, latency histogram, backend
      health gauge, tokens/sec counter per backend
- [ ] Admin API: `GET/POST/DELETE /admin/backends`,
      `GET /admin/backends/status`
- [ ] Per-user spend tracking + quota enforcement (gateway stores tokens;
      proxy enforces quotas against `X-User-Id`)

## Nice to have

- [ ] Request queuing with timeout instead of immediate 503 at capacity
- [ ] Backend auto-discovery (query `/v1/models` on new backend, register
      models automatically)
- [ ] Weighted routing (prefer faster backends)

## Good-citizen: upstream cancellation fix to llama-swap

- [ ] Open PR on `mostlygeek/llama-swap` threading `r.Context()` through
      `ProxyRequest` so client disconnect cancels upstream. ~50 lines,
      separate from anything we build here.

## Open questions

- [ ] Project repo URL (once externalized from `ycluster.local/local-ai-proxy`)
- [ ] Repo location once externalized (separate GitHub repo, not in
      ycluster)
- [x] Config format: YAML + etcd (pluggable Source interface)
- [ ] Minimum Go version at release time (bumped to `go 1.25` because
      etcd client v3.6.10 requires it; revisit at release)
