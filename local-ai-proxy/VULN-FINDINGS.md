# Vulnerability Scan: local-ai-proxy

Static review of `/home/user/ycluster/local-ai-proxy` @ `8f6ab6c`, scanned 2026-08-09.
Candidates only — not execution-verified. Consume with `/triage`.

## Scope

5 focus areas across 15 non-test `.go` source files:

1. HTTP `/v1/*` OpenAI API forward & per-model ACL (handler.go, acl.go, trusted.go, middleware.go)
2. Routing / model body / multipart and body caps (router.go, load.go)
3. Health probes, `/healthz`, `/metrics` (health.go, metrics.go)
4. etcd source & config trust boundary (source.go, etcdclient.go, disabled.go, config.go)
5. CLI ops surface (cli.go)

Read-only scan; no build/run/network. Findings are ranked by confidence; nothing was dropped.

## Summary

- **10 findings** — 1 HIGH, 5 MEDIUM, 4 LOW, 2 low-confidence (< 0.4).

| id | severity | category | file:line | title |
|---|---|---|---|---|
| F-001 | HIGH | auth-bypass | router.go:157 | X-Model header vs body model confusion lets a client bypass the per-model ACL |
| F-002 | MEDIUM | auth-bypass | source.go:158 | etcd watch drops a model's ACL on an invalid PUT while retaining stale backends |
| F-003 | MEDIUM | ssrf | source.go:195 | etcd-derived api_base contacted with client headers (incl. Authorization) forwarded |
| F-004 | MEDIUM | auth-bypass | acl.go:69 | ACL is allow-by-default for models with no matching rule |
| F-005 | MEDIUM | missing-authentication | etcdclient.go:63 | etcd client silently falls back to plaintext when TLS env vars are unset |
| F-006 | MEDIUM | information-disclosure | metrics.go:51 | Unauthenticated /metrics exposes backend URLs and model inventory cluster-wide |
| F-007 | LOW | information-disclosure | handler.go:449 | /healthz echoes backend URLs, probe error strings, and model names |
| F-008 | LOW | auth-bypass | handler.go:222 | Passthrough mode routes with an empty model, so per-model ACL is never enforced |
| F-009 | LOW | key-injection | cli.go:203 | CLI-discovered model IDs written as etcd keys after only weak validation |
| F-010 | LOW | header-injection | handler.go:318 | Broad /v1/* path forwarding and blocklist header pass-through |

---

### F-001 — X-Model header vs body model confusion lets a client bypass the per-model ACL

- **Category:** auth-bypass · **Severity:** HIGH · **Confidence:** 0.8
- **File:** router.go:157

`ModelRouter.Route` (router.go:157) uses the client-supplied `X-Model` header as the routed
model for both ACL and backend selection (router.go:200-206 JSON, router.go:233-259
multipart). The body is buffered but its `model` field is ignored for routing, and the raw
body is forwarded to the backend byte-for-byte (handler.go:303-318). The ACL runs against
`route.Model` — the header value (handler.go:222-233) — while the backend executes the
`model` field still present in the forwarded body. A client can pick an allowed model in the
header yet embed a restricted model in the body and have it served.

**Exploit scenario:** `X-Model: public-model` with body `{"model":"secret-paid-model",...}`
satisfies routing/ACL on `public-model` while the backend serves `secret-paid-model`,
bypassing its ACL. Same for multipart (X-Model vs `model` form field).

**Recommendation:** Derive the effective model from the body (or a fully trusted internal
header), ACL-check the exact model the backend will execute, and on any header/body mismatch
either error out or rewrite the body model before forwarding.

---

### F-002 — etcd watch drops a model's ACL on an invalid PUT while retaining stale backends

- **Category:** auth-bypass · **Severity:** MEDIUM · **Confidence:** 0.6
- **File:** source.go:158

On `EventTypePut` (source.go:158-167), a value that fails `parseKV` (non-JSON, empty
backends, no valid api_base) returns `(nil, nil, false)`. The `if ok` guard (160-162) leaves
`nextM[name]` pointing at the previously-loaded backends, but the `else { delete(nextA,name) }`
branch (165-167) clears the model's ACL because `acl == nil`. Stale backends remain routable
while the ACL disappears, and `ACLConfig.Check` fails open for a model with no rule (acl.go:73-75).

**Exploit scenario:** An etcd writer clobbers a restricted model's key with a malformed value
(classic partial-apply bug); the model stays routable from old backends but the ACL entry is
gone, so callers with no matching allow rule are still granted access.

**Recommendation:** Make watch updates atomic and fail-closed — on a parse failure drop both
the backends and the ACL together (or keep both). Never remove the ACL while retaining
routable backends. Reject malformed values rather than applying them partially.

---

### F-003 — etcd-derived api_base contacted with client headers (incl. Authorization) forwarded

- **Category:** ssrf · **Severity:** MEDIUM · **Confidence:** 0.55
- **File:** source.go:195

`EtcdSource.parseKV` (source.go:189-206) turns `api_base` strings into URLs with no scheme/host
allowlist (`url.Parse` at source.go:195). The health checker GETs `<backend>/v1/models`
(health.go:277-325) and the handler forwards each request with all client headers cloned,
including `Authorization` and `X-User-Id` (handler.go:318, copyHeaders 685-692). A poisoned
`api_base` makes the proxy send outbound HTTP — and the client's credentials — to an arbitrary
internal host. Requires etcd write access (control-plane, threat T6), compounded by the
plaintext etcd fallback (etcdclient.go:63-69).

**Exploit scenario:** A writer sets `api_base` to an internal admin service; a legitimate client
calls that model and the proxy forwards the request (with the client's `Authorization`) to that
host, enabling internal recon and credential/data exfiltration.

**Recommendation:** Schema-validate `api_base` (http/https against an allowlisted host set),
enforce etcd mTLS, and avoid forwarding sensitive client headers to untrusted backends. Treat
etcd writers as a security boundary.

---

### F-004 — ACL is allow-by-default for models with no matching rule

- **Category:** auth-bypass · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** acl.go:69

`ACLConfig.Check` (acl.go:69-87) only denies on an explicit deny match. A model with no ACL
rule (73-75), or a caller matching none of the rule's entries (fall-through 77-86), returns
nil = allow. The main listener is loopback-bound (main.go:152) and TrustedHeadersMiddleware
strips spoofed `X-User-Id` from non-trusted peers (trusted.go:21-28), so the direct no-identity
path is mitigated; the residual gap is an authenticated request that matches no ACL entry
being allowed, and safety depends on the listener staying loopback-bound.

**Exploit scenario:** An authenticated user requests a model with ACL entries but no matching
rule for their identity → allowed. If `Listen` is ever rebound off-loopback, a direct
no-identity request to any model is also allowed.

**Recommendation:** Fail closed on no-match for any model that has at least one rule; verify
every routable model has an explicit allow; keep the listener loopback-bound and require auth
for any non-loopback peer.

---

### F-005 — etcd client silently falls back to plaintext when TLS env vars are unset

- **Category:** missing-authentication · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** etcdclient.go:63

`etcdTLSConfig` (etcdclient.go:63-93) returns `(nil, nil)` — no TLS — whenever `ETCD_CACERT`,
`ETCD_CERT` and `ETCD_KEY` are all absent (67-69), dialing etcd (default http://localhost:2379)
over plaintext. There is no force-TLS mode. The threat model records whether etcd mTLS is
enforced as an open question; this fallback guarantees it is not, purely based on env absence.
When only `ETCD_CACERT` is set, `RootCAs` is nil unless a CA file is present (no CA pinning).

**Exploit scenario:** If ETCD_* are not provisioned, the proxy exchanges model/ACL data in
cleartext and skips server-certificate verification; an on-path/LAN attacker can read or spoof
the source data without detection.

**Recommendation:** Build in an explicit TLS mode, fail startup when mTLS is expected but not
configured, and always load a CA for non-loopback endpoints. Enforce etcd mTLS end-to-end.

---

### F-006 — Unauthenticated /metrics exposes backend URLs and model inventory cluster-wide

- **Category:** information-disclosure · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** metrics.go:51

The metrics registry uses full backend URLs and model names as label values (metrics.go:51-93)
plus live inflight/state gauges. When `metrics_listen` is set, `/metrics` is served on a
dedicated separate listener (main.go:154-158, default :4010 on all interfaces for Prometheus)
with no auth and no firewall restriction visible in the repo. The API listener is loopback-bound,
but this metrics port remains intentionally cross-host-reachable.

**Exploit scenario:** Any cluster-internal host reachable to :4010 GETs /metrics and reads the
backend fleet, model topology, retries, and inflight state — a live service inventory.

**Recommendation:** Restrict :4010 at the firewall to observability hosts, add scrape auth/token,
and confirm nginx never proxies /metrics (or rewrite sensitive labels).

---

### F-007 — /healthz echoes backend URLs, probe error strings, and model names

- **Category:** information-disclosure · **Severity:** LOW · **Confidence:** 0.45
- **File:** handler.go:449

`writeHealthz` (handler.go:418-529) serializes every backend URL, state, timestamp, and probe
error string (449-457, 485-487) plus per-model backend URL lists. It is served on the main,
loopback-bound listener (handler.go:140-149; main.go:152), so today it is local-only and largely
risk-accepted telemetry.

**Exploit scenario:** If `/healthz` is ever proxied or the listener widened, any reachable host
can read backend URLs and raw probe error strings (potential internal hostname/path leakage).

**Recommendation:** Redact URLs and raw error strings in any externally reachable context,
restrict the listener, and confirm nginx never proxies /healthz.

---

### F-008 — Passthrough mode routes with an empty model, so per-model ACL is never enforced

- **Category:** auth-bypass · **Severity:** LOW · **Confidence:** 0.4
- **File:** handler.go:222

`PassthroughRouter.Route` returns a route with `Model` unset (router.go:95-97), so the ACL check
runs `acl.Check("", ...)` (handler.go:222-233), which looks up `a.Models[""]` and returns nil =
allow (acl.go:73-75). No per-model ACL is enforced in passthrough mode.

**Exploit scenario:** A single-backend deployment with an ACL configured forwards every request
regardless of body model, because the empty-key ACL check trivially succeeds.

**Recommendation:** Disallow ACL config in passthrough mode (fail fast), enforce a single-model
allowlist, or parse the body model before forwarding so the ACL can be applied.

---

### F-009 — CLI-discovered model IDs written as etcd keys after only weak validation

- **Category:** key-injection · **Severity:** LOW · **Confidence:** 0.35
- **File:** cli.go:203

`models add <api-base>` without an explicit model calls `discoverModels` (cli.go:166), which GETs
`<api-base>/v1/models` and extracts model IDs from the network response (cli.go:518-549). Each ID
passes only `validateModelName` (cli.go:134-142: rejects empty / "://" only) before being written
to etcd as `key = prefix + model` (cli.go:203). No control-character, separator, or length checks,
so network-derived IDs (from an untrusted/untrusted endpoint) influence the etcd key namespace.

**Exploit scenario:** An operator points the CLI at a backend (or intercepted api-base) that
emits a crafted model ID (slashes, control chars, oversized); it is persisted to etcd unsanitized.

**Recommendation:** Validate model IDs against a strict allowlist charset/length before
persisting, and reject IDs that fail rather than writing them.

---

### F-010 — Broad /v1/* path forwarding and blocklist header pass-through

- **Category:** header-injection · **Severity:** LOW · **Confidence:** 0.35
- **File:** handler.go:318

Any path under `/v1/` is forwarded verbatim (handler.go:308-318) and the client's full header set
is copied through except hop-by-hop via `copyHeaders` (handler.go:318, 685-692); response bodies
stream back verbatim (handler.go:347-350). No per-backend allowlist of paths, methods, or headers.

**Exploit scenario:** A client sends `/v1/admin/...` (forwarded to the backend) or injects
`X-Forwarded-For: 127.0.0.1` to spoof trust in backends keying access off forwarded-for.

**Recommendation:** Replace blocklist header forwarding with an explicit allowlist and add a
per-backend allowlist of permitted `/v1/*` subpaths and methods; strip `X-Forwarded-*` unless
regenerated by a trusted proxy.
