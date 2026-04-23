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

- [x] Periodic health checks per unique backend URL (`GET /v1/models`,
      default 30s, logs state transitions; not yet wired into routing)
- [x] Multiple backends per model (fan-out) with least-loaded selection,
      consulting health state to skip down backends
- [x] In-flight counter per backend (`LoadCounter`, atomic,
      handler-managed around each upstream call)
- [x] Transparent retry on transport error or 4xx/5xx from a backend —
      try each healthy peer at most once; last attempt commits
      whatever it returns. Each failure nudges the health checker to
      re-verify out of band (`HealthChecker.Probe`), so a transient
      glitch doesn't strand traffic waiting for the next tick.
- [ ] Per-backend `max_concurrent` concurrency cap
- [ ] Model aliasing (friendly name → backend model ID, rewrite request
      body)
- [ ] Tests: concurrency cap honored under parallel load

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

## Hardening (deferred — not critical while loopback-only behind nginx)

Runtime:

- [ ] `DynamicUser=yes` in the systemd unit (drop the explicit user-create
      task; transient UID per start). Requires config file readable by
      non-owner — flip to `root:root 0644` or a group.
- [ ] Expand systemd sandbox: `PrivateDevices=yes`,
      `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`,
      `ProtectControlGroups=yes`,
      `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
      `RestrictNamespaces=yes`, `LockPersonality=yes`,
      `MemoryDenyWriteExecute=yes`,
      `SystemCallFilter=@system-service`, `CapabilityBoundingSet=` (empty).
- [ ] `LimitNOFILE=` sized to expected concurrent streams.

HTTP server:

- [ ] `ReadHeaderTimeout` + `IdleTimeout` on `http.Server` (Slowloris
      defense; nginx fronts us today but this is cheap).
- [ ] Explicit `MaxHeaderBytes`.
- [ ] Global `http.MaxBytesReader` guard for non-`ModelRouter` paths
      (ModelRouter already caps at 8 MiB).

Observability / testing:

- [ ] Prom `/metrics` endpoint (loopback-only), with request-rate and
      backend-state gauges — enough to alert on.
- [ ] Integration test asserting `TrustedHeadersMiddleware` strips
      `X-User-Id` from untrusted peers.
- [ ] Log-rate-limit flapping backends so health-checker noise stays
      bounded.
- [ ] Go native `fuzz` targets for the model-name parser and config
      loader.

Supply chain (done):

- [x] `go mod verify` on every build; `-trimpath -buildvcs=false`
- [x] `go 1.25.9` (stdlib CVE fixes; govulncheck clean)
- [x] `vulncheck` / `check` Makefile targets (opt-in govulncheck)
- [x] `.syncignore` excludes `bin/` so deploys never ship a stale local
      artifact

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
