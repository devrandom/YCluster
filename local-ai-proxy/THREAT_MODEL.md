# Threat Model: local-ai-proxy (YCluster inference gateway)

## 1. System context

local-ai-proxy is a Go 1.25 inference gateway (net/http, no framework) that fronts self-hosted LLM backends on a small AI cluster. It presents an OpenAI-compatible API (`/v1/*`): chat completions, completions, embeddings, tokenize/detokenize, and document parsing (`file_parse` → MinerU). It routes each request by model name to one of several backends (vLLM, llama-server, whisperx, MinerU, etc.) using least-loaded fan-out with bounded retries, health-checks each backend, honors operator-disabled backends, and exposes `/v1/models`, `/healthz`, and `/metrics`. An etcd or static-YAML source provides the model→backend mapping and per-model ACLs, hot-reloaded without restart.

Deployment: a publicly-exposed nginx `auth_request` front (OIDC/bearer) forwards to the proxy, which listens on `:4000` (all interfaces) and is reachable on cluster-internal subnets where it is unauthenticated. Identity is handed to the proxy as `X-User-Id` / `X-User-Groups` headers, which it strips unless the peer is in `trusted_proxies` (loopback default, `127.0.0.1/32`,`::1/128`, not overridden). The proxy authenticates nobody itself: `trusted_proxies` prevents header forgery, while per-model ACLs are the only authorization and are **allow-by-default**. The owner treats internal (cluster) misuse as out of scope for now — the ACL gates external users only; the direct cluster-reachable, unauthenticated path is an accepted residual risk.

## 2. Assets

| asset | description | sensitivity |
|---|---|---|
| host process integrity | Proxy runs as an unprivileged user on a core node; Go is memory-safe so the primary blast radius is forwarded untrusted content reaching parsing backends, not the proxy process itself. | high |
| shared GPU/compute backends availability | Backends run on expensive shared GPUs; a client can hold slots, trigger long/expensive generations, or replay bodies across retries, degrading service for all users. | high |
| model serving policy (ACL) integrity | Per-model ACL gates which users/groups may access which models; bypass translates to unauthorized (sensitive or cost-heavy) model access. | high |
| user identity (X-User-Id / X-User-Groups) authenticity | Identity headers injected by trusted nginx feed logging, ACL decisions, and future usage/quota; forgery means ACL bypass and spoofed audit trail. | high |
| user conversational content | Prompts, completions, and uploaded docs/audio pass through the proxy in cleartext HTTP to backends on the cluster network. | medium |
| internal cluster topology | Internal backend hostnames/ports, model inventory, and health state, leaked via `/healthz`, `/metrics`, and some backend response bodies. | medium |
| etcd config integrity + availability | Model/disabled/ACL config in etcd; a poisoned models or disabled entry changes routing/availability and ACL writes define who may use which model. | high |

## 3. Entry points & trust boundaries

| entry_point | description | trust_boundary | reachable_assets |
|---|---|---|---|
| HTTP /v1/* OpenAI-compatible API | Route by model name (JSON body, X-Model header, or multipart form field), ACL-gate on X-User-Id/X-User-Groups, forward body to selected backend with streaming SSE passthrough. file_parse routes to MinerU. | unauth-HTTP-via-nginx → authenticated session + internal backend forward; direct cluster access bypasses nginx/auth | model serving policy (ACL) integrity, user identity (X-User-Id / X-User-Groups) authenticity, user conversational content, shared GPU/compute backends availability, internal cluster topology |
| GET /v1/models | Synthesized list of servable (healthy, non-disabled) models, filtered by ACL when configured. | authenticated user → model inventory disclosure | internal cluster topology, model serving policy (ACL) integrity |
| GET /healthz | Unauth per-backend + per-model health rollup; probes auth sidecar 127.0.0.1:4002/healthz and reports auth status; exposes internal backend URLs, model names, and last-check errors. | unauth → internal topology/availability disclosure (intended internal-only) | internal cluster topology, shared GPU/compute backends availability |
| GET /metrics (Prometheus) | Separate mux, bypasses logging and TrustedHeadersMiddleware; exposes internal backend URLs and model names as label values plus inflight gauges. | loopback/direct-network → internal topology disclosure (intended internal-only) | internal cluster topology, shared GPU/compute backends availability |
| Backend probes (health checker) | Periodic GET <backend>/v1/models to every configured backend URL (operator-controlled from YAML/etcd) and out-of-band Probe() after retry failures (bare context.Background, no timeout). | operator-config (etcd/YAML) → arbitrary internal HTTP | internal cluster topology, host process integrity |
| etcd source: models + disabled prefix + ACL | EtcdSource watches prefix for model→backend JSON; inline ACL rules merged with YAML; disabled-backend list read from sibling prefix each health cycle. Writable via CLI and control-plane nodes. | control-plane etcd writers → routing + ACL policy | etcd config integrity + availability, model serving policy (ACL) integrity, shared GPU/compute backends availability |
| CLI ops surface | models add/remove, backends disable/enable, acl set subcommands that read config and write etcd; models add auto-discovers /v1/models from an operator-supplied api-base URL via arbitrary HTTP GET. | operator terminal → etcd mutation + arbitrary outbound GET | etcd config integrity + availability, internal cluster topology |
| Config file (YAML) + env | Listen addr, backends, trusted_proxies CIDRs, ACL, caps; systemd EnvironmentFile=/etc/ycluster/etcd-client.env provides etcd env. | root/operator + /etc → identity/ACL/routing policy + TLS trust | user identity (X-User-Id / X-User-Groups) authenticity, model serving policy (ACL) integrity, etcd config integrity + availability |
| systemd unit (unprivileged user) | Unprivileged User, NoNewPrivileges, ProtectSystem/ProtectHome/PrivateTmp; deferred hardening (DynamicUser, PrivateDevices, ProtectKernel*, RestrictAddressFamilies, MemoryDenyWriteExecute, SystemCallFilter, CapabilityBoundingSet). | local compromise → kernel/device address-space exposure | host process integrity, user conversational content |
| nginx auth_request front | nginx does bearer/OIDC auth, injects X-User-Id from a trusted proxy, strips Authorization, client_max_body_size 50M, long 10m timeouts; forwards to loopback proxy. | internet client → nginx auth → trusted proxy on loopback | user identity (X-User-Id / X-User-Groups) authenticity, model serving policy (ACL) integrity, user conversational content |
| etcd backend (cluster state) | Models/disabled/ACL in etcd under /cluster/config/inference/...; client uses mTLS when ETCD_CERT/ETCD_KEY set, plaintext fallback when unset (dev). | control-plane trust root → routing/ACL policy | etcd config integrity + availability, model serving policy (ACL) integrity |

## 4. Threats

| id | threat | actor | surface | asset | impact | likelihood | status | controls | evidence |
|---|---|---|---|---|---|---|---|---|---|
| T1 | Direct (non-nginx) unauthenticated access to the proxy from cluster-internal subnets grants model access, bypassing nginx auth and ACL gating | adjacent_network | nginx auth_request front, HTTP /v1/* OpenAI-compatible API | model serving policy (ACL) integrity, user identity (X-User-Id / X-User-Groups) authenticity, user conversational content, shared GPU/compute backends availability | critical | possible | risk_accepted | trusted_proxies strips forged identity (prevents forgery, not anonymous access); ACL is allow-by-default; no proxy-side auth |  |
| T2 | Exhaustion of shared GPU/compute backends via unbounded generation parameters, large multipart uploads, long-lived streaming, or retry replay | remote_auth | HTTP /v1/* OpenAI-compatible API | shared GPU/compute backends availability, host process integrity | high | likely | partially_mitigated | 32MiB JSON / 200MiB multipart body caps, 4096-char model form field, retry limited to 5xx/429/404; per-user inflight/param caps planned | 873a244 |
| T3 | Server-side request forgery / outbound data exfiltration via crafted document parsing (MinerU) or multimodal image_url fetch reaching internal services | remote_auth | HTTP /v1/* OpenAI-compatible API | host process integrity, user conversational content, internal cluster topology | high | possible | partially_mitigated | OS-level egress sandbox for MinerU (owner: separate review task); app-level SSRF disable for *-http-client/server_url; multimodal image_url fetch on other backends still to verify | 2ddc556, 873a244 |
| T5 | External authenticated users reach sensitive or cost-heavy models they are not entitled to because ACL is allow-by-default and coverage is unverified | remote_auth | HTTP /v1/* OpenAI-compatible API, GET /v1/models | model serving policy (ACL) integrity | high | possible | partially_mitigated | per-model ACL on X-User-Id/X-User-Groups when configured; no fail-closed default; internal misuse explicitly out of scope |  |
| T6 | etcd config poisoning: writes to models/disabled/ACL keys change routing, ACL, or availability policy | insider | etcd source: models + disabled prefix + ACL, etcd backend (cluster state), CLI ops surface | etcd config integrity + availability, model serving policy (ACL) integrity, shared GPU/compute backends availability | high | rare | partially_mitigated | all control-plane nodes trusted writers; mTLS when ETCD_CERT/ETCD_KEY set (enforcement unverified); plaintext fallback possible | 63ade7f |
| T7 | Privilege escalation from a compromised proxy process to host capabilities because systemd sandbox facets are not applied | local_admin | systemd unit (unprivileged user) | host process integrity, user conversational content | high | rare | partially_mitigated | unprivileged User, NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp; DynamicUser/ProtectKernel*/SystemCallFilter deferred (planned) |  |
| T4 | Spoofed or replayed X-User-Id / X-User-Groups headers bypass per-model ACLs and forge identity in logs | adjacent_network | nginx auth_request front, GET /v1/models, HTTP /v1/* OpenAI-compatible API | user identity (X-User-Id / X-User-Groups) authenticity, model serving policy (ACL) integrity | high | rare | mitigated | trusted_proxies loopback default strips identity headers from any non-loopback peer; trust is IP/CIDR-based |  |
| T8 | Internal cluster topology (backend hostnames, ports, model inventory, health errors) disclosed via /healthz, /metrics, and backend response bodies | remote_unauth | GET /healthz, GET /metrics (Prometheus), HTTP /v1/* OpenAI-compatible API | internal cluster topology | medium | likely | partially_mitigated | metrics on separate mux; internal reachability intended/risk-accepted; backend response URLs not rewritten | b4e9a50 |
| T9 | Cleartext or forged reads of routing/ACL config and conversational content from non-mTLS etcd and loopback HTTP | adjacent_network | etcd backend (cluster state), Config file (YAML) + env | etcd config integrity + availability, user conversational content, user identity (X-User-Id / X-User-Groups) authenticity | medium | possible | partially_mitigated | mTLS capability exists; plaintext fallback when env unset (enforcement unverified) | 63ade7f |
| T10 | Broad /v1/* pass-through lets authenticated clients reach backend internal or admin endpoints and leak backend state | remote_auth | HTTP /v1/* OpenAI-compatible API | internal cluster topology, host process integrity, shared GPU/compute backends availability | medium | possible | partially_mitigated | path rewrite limited to /v1/*; blocklist header forwarding (not allowlist) | 873a244 |
| T11 | Slowloris or connection-exhaustion DoS against the HTTP server from missing ReadHeaderTimeout/IdleTimeout/MaxHeaderBytes and no global body cap | remote_unauth | HTTP /v1/* OpenAI-compatible API, GET /healthz, GET /metrics (Prometheus) | shared GPU/compute backends availability, host process integrity | medium | possible | partially_mitigated | nginx front with long 10m timeouts; server timeouts/global body cap deferred (planned soon) | 5ae2178 |
| T12 | Operator-driven or poisoned config causes probes to hit arbitrary internal URLs (SSRF-ish) and hang without a timeout (bare context.Background) | insider | Backend probes (health checker), CLI ops surface, Config file (YAML) + env | internal cluster topology, host process integrity | low | rare | partially_mitigated | probe URLs are operator-controlled; no timeout on out-of-band Probe() | 5e90d89 (minor) |
| T13 | Health/metrics and ops activity occurs without attributable audit logging (metrics mux bypasses the request logger) | local_admin | GET /metrics (Prometheus), CLI ops surface | etcd config integrity + availability, user identity (X-User-Id / X-User-Groups) authenticity | low | rare | unmitigated | per-request logging on main mux only |  |

## 5. Deprioritized

| threat | reason |
|---|---|
| Direct (non-nginx) cluster access to the unauthenticated proxy (T1) | risk accepted by owner [Owner-states]: internal/cluster misuse is not a current concern; ACLs are not intended to protect against internal misuse. Revisit if external lateral movement into the cluster becomes a realistic path. |
| Internal `/healthz` and `/metrics` topology/availability disclosure (part of T8) | risk accepted by owner [Owner-states]: reachability is intended to be internal to the cluster only. Verify nginx does not expose these publicly. |
| Supply-chain compromise of dependencies | out of scope: build uses vendored/pinned approved sources, go mod verify, -trimpath, and govulncheck target |
| In-transit tampering of end-user content on the proxy-to-backend leg | risk accepted: traffic stays on the trusted cluster network (adjacent-only threat), and mTLS/topology controls cover the higher-value etcd path |
| Repudiation of individual end-user requests on /v1/* | mitigated: per-request logging attributes requests to X-User-Id; the residual /metrics and ops audit gap is captured as a threat row |

## 6. Open questions

- [Owner-states] Is etcd mTLS actually enforced cluster-wide, or is the plaintext fallback live? Owner stated all control-plane nodes can write the inference prefix (insider trust). Verify by: check etcd TLS config / ETCD_CERT/ETCD_KEY on control plane. Affects: T6, T9.
- [Owner-states] MinerU is egress-sandboxed at the OS level; that review is a separate task and is not tracked here. Confirm the sandbox is enforced in prod and confirm multimodal image_url (and other backends') SSRF posture remains covered. Affects: T3, T8.
- Do MinerU (and other backends) still return internal `status_url`/`result_url` hosts to public users (the b4e9a50 leak)? Verify by: call `/v1/file_parse` and inspect response. Affects: T8.
- Are all externally-reachable sensitive/cost-heavy models covered by explicit ACL rules given allow-by-default? Owner: ACLs are for external users; coverage unverified. Verify by: compare routable model list to ACL policy. Affects: T5.
- Confirm nginx does not publicly proxy `/healthz` and `/metrics` (owner intent is internal-only). Affects: T8.
- Confirm `trusted_proxies` remains at the loopback default in all prod environments (drives T4 mitigation). Affects: T4.
- [Owner-states] Hardening timeline: server timeouts + global body cap, per-user inflight/param caps, and systemd sandbox expansion are planned "soon" — confirm target date. Affects: T2, T7, T11.
- [Owner-states] Accept T1 (internal misuse) only while external lateral movement into the cluster is not a realistic path — revisit if the cluster attack surface changes. Affects: T1.

## 7. Provenance

- mode: bootstrap-then-interview
- date: 2026-08-09
- target: /home/user/ycluster/local-ai-proxy @ 4cf735d
- inputs: git-log + CHANGELOG mined (none supplied via --vulns); seed THREAT_MODEL.md
- owner: present, unnamed

## 8. Recommended mitigations

| mitigation | threat_ids | closes_class | effort |
|---|---|---|---|
| Only trust identity headers from a single loopback nginx via socket source (not IP/CIDR alone); enforce fail-closed ACL on every routable model | T1,T4,T5 | partial | M |
| Enforce per-user inflight/concurrency caps and cap generation parameters (max_tokens, n, top_logprobs) at the proxy; bound retry replay | T2,T10 | partial | M |
| Sandbox document-parsing backends at the OS level (egress deny + network namespace); switch header forwarding to an allowlist | T3,T10 | partial | L |
| Strip/rewrite internal hostnames and URLs from all backend response bodies at the proxy boundary; hide backend URL/model/health detail from publicly reachable paths | T8,T10 | partial | M |
| Require mTLS for etcd cluster-wide and schema-validate model/ACL JSON at the source, rejecting unknown or unsafe values | T6,T9 | partial | M |
| Remove plaintext etcd fallback in prod and enable TLS at the nginx→proxy hop | T1,T9 | partial | S |
| Restrict forwarded paths/verbs and headers/query params to a strict per-backend allowlist | T3,T10 | partial | M |
| Set ReadHeaderTimeout/IdleTimeout/MaxHeaderBytes and a global MaxBytesReader for all routes | T11,T2 | partial | S |
| Bind loopback-only, add defense-in-depth auth for any non-loopback traffic, and enable TLS at the nginx→proxy hop | T1,T4 | partial | M |
| Expand the systemd sandbox (DynamicUser, RestrictAddressFamilies, ProtectKernel*, MemoryDenyWriteExecute, SystemCallFilter, empty CapabilityBoundingSet) | T7 | partial | M |
| Route all observable endpoints (incl. /metrics and health probes) through consistent attributed logging | T13 | partial | S |
