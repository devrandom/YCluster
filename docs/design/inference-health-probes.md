# Inference backend health probes

## Status

The exo-aware probe and the model-aware rollups are implemented. The
reactive 404 safety net is not: it needs a per-model stale signal
plumbed through `/healthz` and the model metrics, which is a separate
change.

## Problem

`local-ai-proxy` probes every backend with a single request:

```
GET <backend>/v1/models
```

Any status below 500 counts as healthy, and `data[].id` becomes the
backend's served-model list (`local-ai-proxy/health_check.go:15`). The
router then requires `IsHealthy && ServesModel` before forwarding
(`local-ai-proxy/router.go:310`).

That is the right contract for backends whose `/v1/models` reflects what
they can currently serve (vLLM, SGLang, TEI). It is the wrong contract
for exo: exo's `/v1/models` is a **model catalog**, not a placement
report. It returns every known card (120 entries) whether or not an
instance is placed, and returns 200 with zero runners.

Consequences:

- A backend with **no placed instance** is reported `healthy`.
- `ServesModel` passes, so the router forwards work to a backend that
  can only answer `404 No instance found for model …`.
- `local_ai_proxy_model_healthy_backends` stays at 1, so neither
  `InferenceBackendDown` nor `InferenceModelDown` fires.
- `ycluster inference status` and `/healthz` both report `ok`.
- The only trace is the per-request 404 and the request log. Placement
  failure causes no health-state transition, so nothing is logged as a
  health event.

A real outbound-network outage on the exo nodes produced exactly this:
the proxy stayed green while every Kimi request returned 404. The probe
was reachable, so it kept saying `healthy`; it simply never asked the
question that mattered.

`handler.go:319` already treats a backend 404 as retryable for
"model-placement drift", and fires an out-of-band `Probe` — but with a
single backend there is no peer to retry, and the probe re-reads the
same placement-blind catalog.

## Signals available from exo

Measured against a two-node exo cluster with K2.6 placed:

| Endpoint | Size | Latency | Meaning |
|---|---:|---:|---|
| `/v1/models` | 54,312 B | 11 ms | 120-model catalog; ignores placement |
| `/v1/models?status=downloaded` | 941 B | 5 ms | downloaded models (still not placed) |
| `/state/instances` | 2,767 B | 29 ms | placed instances, including model IDs and runner IDs |
| `/state/runners` | 117 B | 17 ms | runner state machine (`RunnerReady`, `RunnerLoading`, …) |
| `/ollama/api/ps` | 184 B | 4 ms | placed model IDs (no readiness) |

`/state` itself is not a probe candidate: it serialises the whole state
(265 KB when quiet, and it has grown to tens of MB with a bloated event
log). `/state/instances` and `/state/runners` are the cheap sub-objects
of the same state (`/state/{path}` route).

`status=downloaded` is insufficient: a model can be on disk and still
have no instance, which is the failure mode above. `/ollama/api/ps`
names placed models but lists an instance as soon as placement is
issued, before its runners are ready.

## Options

| Option | Signal | Cost | Verdict |
|---|---|---|---|
| **Exo state probe** | instances + runner readiness + model IDs | 2 tiny GETs per interval | **Recommended.** No model load, catches no-instance and loading. |
| Synthetic completion canary | end-to-end generation | ~2 s + a generation slot per probe | Useful safety net for ready-but-broken models; too expensive as the primary probe. |
| Reactive 404 tracking | real request failures | negligible | Good complement; cannot catch a silent outage with no traffic. |
| Patch exo `/v1/models` | placement-aware catalog | upstream patch | Breaks catalog consumers (dashboard, model browser); not first choice. |
| Do nothing | request logs only | zero | Rejected; this incident stayed invisible for days. |

## Recommended design

Make the probe backend-aware. The default remains `/v1/models`; exo
backends additionally must show a placed, ready instance.

### Selection

Use the `/v1/models` payload itself: exo sets `owned_by: "exo"` on every
entry. Each probe already fetches and parses that payload, so when it
reports `owned_by: "exo"` the checker runs the state probe instead of
recording the catalog as healthy. Detection repeats every tick, which
keeps it self-correcting if a backend changes type; the extra cost is
the catalog fetch the probe already made.

If the field is absent, keep the current behaviour, so a future exo
version that drops `owned_by` degrades to today's probe rather than
breaking. An explicit per-backend override map in YAML
(`backend_health: {<url>: exo}`) is the escape hatch if the magic is
unwanted; it costs a config field and an Ansible template entry but no
per-request logic.

### Probe

For an exo backend:

1. `GET /state/instances` (body capped at 1 MiB, same as today).
2. `GET /state/runners` (small; parse defensively).
3. For each instance, read `shardAssignments.modelId` and the
   `runnerToShard` keys. An instance is ready when every referenced
   runner is `RunnerReady` or `RunnerRunning` — exo's planner
   dispatches in both states, and `RunnerRunning` is the steady state
   while a generation is in flight.
4. State:
   - parse/transport failure: `down`, with the error in `Err`;
   - no instances: `down`, `Err: "no instance placed"`;
   - instances but none ready: `down`, `Err: "runners not ready"`;
   - at least one ready instance: `healthy`.
5. `Models` = model IDs of **ready** instances. This repairs
   `ServesModel`, so a model with no ready instance is filtered from
   routing and drives `local_ai_proxy_model_healthy_backends` to 0.

An unparsable state response must be `down`, not `healthy`. The current
`/v1/models` path deliberately leaves `Models` nil on parse failure to
avoid filtering a backend it could not interrogate; that leniency is
what let the catalog pass. For an exo state probe the state is the whole
point, so a failed parse is a failed check.

### Alerting

Unchanged: `InferenceBackendDown` fires after 5 minutes of all proxies
reporting `down`, and `InferenceModelDown` after 5 minutes of zero
healthy backends. The normal K2.6 reload window is ~2 minutes, so a
restart or re-placement does not page. A failed placement does.

Because the router now sees `IsHealthy == false`, clients get the
proxy's `503 no healthy backend` instead of a forwarded 404 with an exo
error body. Both are failures, but the 503 is accurate and does not
imply the model name is wrong.

### Reactive 404 safety net

Separately, track consecutive upstream 404s per `(backend, model)`.
`retryableStatus` already classifies 404 as placement drift and fires a
`Probe`; after the probe is placement-aware the state usually flips on
its own. If it does not (e.g. a backend that reports ready but answers
404), after N consecutive 404s (say 3) mark the model unhealthy for
routing and emit a metric/log line. This is cheap and closes the gap
between "probe says ready" and "the model actually answers".

Do not count 404s as backend-down by themselves: a 404 can be a stale
model mapping, which is per-model, not per-backend.

### Per-model rollups ignore `ServesModel`

There is a second, independent blind spot behind the same outage shape.
`ServesModel` filters *routing* (`router.go:320`), but the model rollups
only look at backend state:

- `local-ai-proxy/health.go:191` (`updateModelMetrics`) counts a backend
  as healthy for a model whenever `bh.State == StateHealthy`, without
  checking `bh.Models`.
- `local-ai-proxy/handler_response.go:17` (`modelStateFromBackends`)
  does the same for `/healthz` and `servableModels`.

So if an exo backend has one ready instance and one unplaced model, the
unplaced model's backend still counts as healthy: it disappears from
routing (503) but `InferenceModelDown` and `/healthz` stay green. Fixing
the probe alone would catch the "no instances at all" case; fixing the
rollups is required for the partial case and for any backend whose
`/v1/models` list is narrower than the etcd mapping.

The shared fix is a helper that answers, for one model, "is this
backend state healthy *and* does it serve this model?", treating a nil
`Models` (list not determinable) as "serves it". Use it in
`updateModelMetrics`, `modelStateFromBackends`, and `servableModels`.

## Implementation sketch

- `local-ai-proxy/health_check.go`: dispatch to `checkExo` when the
  catalog reports `owned_by: exo`; keep `checkOne` for everything else.
- `local-ai-proxy/health.go`: add the model-aware `backendServesModel`
  helper and use it in `ServesModel` and `updateModelMetrics`.
- `local-ai-proxy/handler_response.go`: make `modelStateFromBackends`
  take the model name and apply the same helper, so `/healthz` and
  `servableModels` agree with routing.
- Tests (`health_test.go`):
  - exo fixture with zero instances -> `down`, `Models` empty non-nil;
  - one instance with `RunnerLoading` -> `down`;
  - one instance with two `RunnerReady` runners -> `healthy`,
    `Models == ["…/Kimi-K2.6-mlx-DQ3_K_M-q8"]`;
  - all-`RunnerRunning` and mixed `RunnerReady`/`RunnerRunning` ->
    `healthy`;
  - malformed state body -> `down`;
  - non-exo `/v1/models` backend -> unchanged behaviour;
  - one ready and one unplaced model on the same backend -> the
    unplaced model is `unavailable` in the rollup and absent from
    `model_healthy_backends`.
- Docs: `local-ai-proxy/docs/deploy.md` (probe semantics) and
  `docs/operations/exo.md` (why exo needs it).

## Validation

1. Unit tests above; `go test ./...` in `local-ai-proxy`.
   A prototype of the parse against a live two-node exo cluster returned
   the placed model with both runners `RunnerReady`.
2. Live check against an exo backend with K2.6 placed: probe reports
   healthy and the model list contains only the placed model.
3. Failure check without touching the shared instance: point the proxy
   unit test at an httptest server serving the exo state fixtures. For a
   cluster check, use `ycluster inference disable` on a test backend
   rather than deleting the live instance.
4. Confirm `InferenceBackendDown` fires for a deliberately unplaced
   backend in a dev/test environment, and does **not** fire across a
   normal re-placement.

## Interactive latency probe

The inference dashboard has a synthetic interactive-latency panel fed by
`monitoring/install-prometheus.yml`. It is separate from routing health: it
answers "how long does a small request take right now?", not "can this backend
serve the model?".

- The collector runs on the storage leader only. Non-leaders remove their
  textfile so failover leaves one live sample stream.
- Targets are discovered from Prometheus: every active vLLM/SGLang
  `(backend, model)` pair with an e2e request-latency histogram.
- Each target gets one excluded warmup and ten sequential measured requests
  with a unique prompt targeting ~64 tokens, 32 generated tokens, temperature 0,
  thinking disabled, and `ignore_eos`.
- The probe talks directly to the backend rather than through the gateway, so
  the metric is not diluted by long user-request durations. It still shares
  the engine with live traffic; queueing and contention remain visible.
- Output is `inference_interactive.prom` in the node_exporter textfile
  directory, with P50 TTFT/e2e plus sample, failure, token-count, and
  success timestamp gauges.
- The Grafana panel is "Interactive P50 — synthetic small probe" on the
  Inference dashboard.
