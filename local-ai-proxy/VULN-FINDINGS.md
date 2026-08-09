# Vulnerability Scan: local-ai-proxy

Static review of `/home/user/ycluster/local-ai-proxy` @ `f8b2a6f`, scanned 2026-08-09.
Re-emitted after `git pull` (4cf735d..f8b2a6f) — the pulled commit binds the API
listener to loopback (`127.0.0.1:4001`) and moves `/metrics` to a dedicated `:4010`
listener. Candidates only — not execution-verified. Consume with `/triage`.

## Scope note

MinerU is **out of scope** for this scan: it lives in its own service location with its
own sandbox, and the internal `status_url`/`result_url` leak originates in what MinerU
emits, not in code in this directory. It is tracked separately (threat T3). Only the
proxy's own in-scope forward/pass-through behavior is covered here.

## Summary

- **4 findings** — 0 HIGH, 3 MEDIUM, 1 LOW, 0 low-confidence.

| id | severity | category | file:line | title |
|---|---|---|---|---|
| F-001 | MEDIUM | auth-bypass | acl.go:69 | Allow-by-default ACL still lets models without an explicit allow be reached (direct /v1 surface now loopback-mitigated) |
| F-002 | MEDIUM | information-disclosure | handler.go:418 | /metrics remains exposed cross-host on a dedicated listener (:4010); /healthz now loopback-bound |
| F-003 | MEDIUM | ssrf | source.go:195 | etcd-backend api_base URLs contacted with client headers forwarded (SSRF + Authorization forwarding) |
| F-004 | LOW | header-injection | handler.go:318 | Broad /v1/* path forwarding and blocklist header pass-through expose backend endpoints |

---

### F-001 — Allow-by-default ACL still lets models without an explicit allow be reached

- **Category:** auth-bypass · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** acl.go:69

`ACLConfig.Check` only denies when an explicit `ACLDeny` matches; any request with no
matching entry falls through to allow. Commit f8b2a6f binds the API listener to loopback,
removing the direct cluster-internal `/v1/*` surface in the managed deployment. The
residual allow-by-default gap (T5) remains: an authenticated nginx request matching no
ACL entry for the requested model is still allowed, and safety depends on the listener
staying loopback-bound.

**Exploit scenario:** An authenticated user requests a model whose ACL has entries but
none matching their identity -> allowed. If `Listen` is ever rebound to a non-loopback
address, the direct no-identity path returns.

**Recommendation:** Fail closed on no-match for any model that has at least one rule;
verify every routable model has an explicit allow; keep the listener loopback-bound.

---

### F-002 — /metrics remains exposed cross-host on :4010; /healthz now loopback-bound

- **Category:** information-disclosure · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** handler.go:418

`writeHealthz` serializes every backend URL/state/error and, as of f8b2a6f, is served on
the now-loopback-bound main listener (mitigated). `/metrics` is served on a dedicated
`metrics_listen` (`:4010`, all interfaces) for cross-host Prometheus scraping (main.go:154-158;
Ansible `inference_proxy_metrics_port` 4010), with no auth and no firewall rule in the repo
restricting it to Prometheus hosts; it still exposes backend URLs and model names as label
values plus live inflight/state gauges.

**Exploit scenario:** Any cluster-internal host reachable to `:4010` can GET `/metrics`
for backend inventory, model names, and real-time state.

**Recommendation:** Restrict `:4010` at the firewall to observability hosts (or add scrape
auth/token); keep `/healthz` loopback-only; confirm nginx doesn't proxy either path.

---

### F-003 — etcd-backend api_base URLs contacted with client headers forwarded

- **Category:** ssrf · **Severity:** MEDIUM · **Confidence:** 0.5
- **File:** source.go:195

`EtcdSource.parseKV` turns `api_base` strings into URLs with no scheme/host allowlist. The
health checker GETs `<backend>/v1/models` and the handler forwards each request with all
client headers cloned (including `Authorization`/`X-User-Id`). All control-plane nodes can
write the inference prefix and etcd mTLS enforcement is unverified (plaintext fallback
exists). A poisoned `api_base` redirects the proxy, and its forwarded credentials, to an
arbitrary internal HTTP service. Rooted in this directory's etcd-source and handler code.

**Exploit scenario:** A writer sets `api_base` to an internal admin service; a legitimate
client calls that model and the proxy forwards the request (with the client's
`Authorization`) to the attacker-chosen host.

**Recommendation:** Schema-validate `api_base` (http/https to an allowlisted host set),
enforce etcd mTLS, and avoid forwarding sensitive headers to untrusted backends.

---

### F-004 — Broad /v1/* path forwarding and blocklist header pass-through

- **Category:** header-injection · **Severity:** LOW · **Confidence:** 0.45
- **File:** handler.go:318

Any path under `/v1/` is forwarded verbatim to the chosen backend, and the client's full
header set is copied through except hop-by-hop/Content-Length. Backend response bodies are
streamed back verbatim (handler.go:347-350). No per-backend allowlist of downstream paths,
methods, or headers exists.

**Exploit scenario:** A client sends `/v1/admin/...`, which the `/v1/*` prefix check passes
and forwards; it can also set `X-Forwarded-For` to spoof trust in backends keying access off
forwarded-for.

**Recommendation:** Replace blocklist header forwarding with an explicit allowlist and add a
per-backend allowlist of permitted `/v1/*` subpaths and methods.
