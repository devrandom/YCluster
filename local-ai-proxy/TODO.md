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
- [x] YAML config (listen addr, single backend URL) with flag overrides
- [x] Structured request log via `slog` (method, path, status, duration,
      bytes, `X-User-Id` when present)
- [x] Graceful shutdown on SIGTERM/SIGINT with 30s drain cap and
      force-close fallback
- [x] End-to-end smoke tested against real vLLM (MiniMax-M2.7): models,
      chat completion, streaming, and disconnect → backend slot freed
- [x] `-race` clean

## Near-term — make single-backend path respectable

- [ ] Deploy as a shadow/canary in front of one existing backend
      (systemd unit + Ansible role + nginx `auth_request` recipe)
- [ ] Auth middleware: trust `X-User-Id` from upstream, static bearer
      fallback map
- [ ] Error response shape compatible with OpenAI
      (`{"error": {"message", "type", "code"}}`)

## Multi-backend — the second differentiator

- [ ] Backend struct: name, URL, `max_concurrent`, models, weight,
      health_interval
- [ ] Router: model name → list of backends; pick healthy + under cap
      with lowest in-flight; 503 when all full
- [ ] In-flight counter per backend
- [ ] Periodic health checks (`GET /v1/models` or configurable)
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
- [ ] Config hot-reload (watch file or SIGHUP)
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
- [ ] Config format: YAML only / API only / both (YAML seed + admin API
      for runtime edits is the current assumption)
- [ ] Repo location once externalized (separate GitHub repo, not in
      ycluster)
- [ ] Minimum Go version at release time (currently `go 1.24`; revisit
      when/if we adopt stable `testing/synctest` on 1.25+)
