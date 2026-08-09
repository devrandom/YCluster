# Triage Report

10 in -> 0 duplicates, 3 false positives, 7 confirmed (0 H / 4 M / 3 L), 2 need manual test.

Context: auto; environment = /v1 API is untrusted HTTP behind authenticated nginx, forwarded to loopback proxy (auth gateway), management surfaces internal-only (owner risk-accepted), etcd/CLI operator-controlled insider trust; scoring = derived H/M/L; 1-pass sequential verification (skill fallback).

## Act on these

### [MEDIUM] X-Model header vs body model confusion lets a client bypass the per-model ACL  (f001)
`router.go:157` | auth-bypass | claimed HIGH (alignment -1) | confidence 8/10
**Owner:** top committer: Devrandom (8/8 recent commits on router.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** needs_manual_test, votes {TP 1, FP 0, CV 0}
**Preconditions (3):**
- Attacker must reach /v1/* through the authenticated nginx front (authenticated remote access)
- The routed (header) backend must itself serve the restricted model named in the body
- Attacker must know a restricted model id and be able to set X-Model + body model independently
**Threat-model match:** T5 (ACL integrity / unauthorized model access)
**Why:** `ModelRouter.Route` (router.go:157, 200-206) derives `route.Model` from the client `X-Model` header while the raw JSON/multipart body is forwarded byte-for-byte (handler.go:303, 312-318). The ACL gate (handler.go:225) checks `route.Model` only, so a client can set X-Model to an allowed model and put a restricted model in the body, which the backend executes. Highest-signal finding; derived MEDIUM rather than claimed HIGH because exploitation needs the backend to actually serve the body model + knowledge of a restricted model id — confirm with a runtime PoC.
**Reachability evidence:** handler.go:206 (Route) → router.go:157 (X-Model read) → handler.go:225 (ACL) → handler.go:303/312-318 (raw body forward)
> Recommend a human build a PoC; static reasoning hit its limit.

### [MEDIUM] etcd watch drops a model's ACL on an invalid PUT while retaining stale backends  (f002)
`source.go:158` | auth-bypass | claimed MEDIUM (alignment 0) | confidence 6/10
**Owner:** top committer: Devrandom (6/6 recent commits on source.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** exploitable, votes {TP 1, FP 0, CV 0}
**Preconditions (3):** write access to inference etcd prefix (insider/config channel); value fails parseKV on an existing restricted model key; allow-by-default Check fails open for the ruleless model.
**Threat-model match:** T6 (etcd config poisoning)
**Why:** On a PUT where `parseKV` fails (acl nil), source.go:160-162 keeps the stale backends in `nextM` while 165-167 deletes `nextA[name]`, so a restricted model stays routable but with its ACL silently removed; `ACLConfig.Check` then fails open (acl.go:73-75). Insider/config-channel trigger, logged as a warning. Fix: apply watch updates atomically and fail closed (drop both backends+ACL or keep both on parse failure).
**Reachability evidence:** source.go:158 → 160-162 (stale nextM) → 163-167 (delete nextA) → acl.go:73-75 (allow)

### [MEDIUM] etcd-derived api_base contacted with client headers (incl. Authorization) forwarded  (f003)
`source.go:195` | ssrf | claimed MEDIUM (alignment 0) | confidence 5.5/10
**Owner:** top committer: Devrandom (6/6 recent commits on source.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** exploitable, votes {TP 1, FP 0, CV 0}
**Preconditions (4):** etcd write to set poisoned api_base; a client calls that model; attacker-selected internal host reachable with a valuable endpoint; client presents a forwardable Authorization header.
**Threat-model match:** T6 (etcd config poisoning)
**Why:** `parseKV` builds backend URLs via `url.Parse` with no scheme/host allowlist (source.go:189-206); the handler copies all client headers incl. Authorization (handler.go:318, 685-692) and the health checker GETs the unvalidated URL (health.go:277-325). Base severity LOW (many preconditions, insider writer), raised to MEDIUM by the T6 match. Fix: schema-validate api_base to an allowlisted host set, enforce etcd mTLS, stop forwarding sensitive headers to untrusted backends.
**Reachability evidence:** source.go:195 → handler.go:318 (Authorization) → health.go:277-325 (probe)

### [MEDIUM] ACL is allow-by-default for models with no matching rule  (f004)
`acl.go:69` | auth-bypass | claimed MEDIUM (alignment 0) | confidence 5/10
**Owner:** top committer: Devrandom (5/5 recent commits on acl.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** exploitable, votes {TP 1, FP 0, CV 0}
**Preconditions (3):** authenticated external user whose identity matches no ACL rule; model gated or caller matches neither allow nor deny; request reaches the proxy via loopback nginx.
**Threat-model match:** T5 (ACL integrity / unauthorized model access)
**Why:** `ACLConfig.Check` returns nil (allow) when the model has no rule (acl.go:73-75) or no entry matches (77-86). The loopback bind (main.go:152) and TrustedHeaders stripping (trusted.go:21-28) remove the direct no-identity vector, but an authenticated request matching no ACL rule is still allowed — the residual external ACL gap. Fix: fail closed for any model that has rules.
**Reachability evidence:** acl.go:73-75 → 77-86 → handler.go:225; mitigant main.go:152 (loopback)

### [LOW] Unauthenticated /metrics exposes backend URLs and model inventory cluster-wide  (f006)
`metrics.go:51` | information-disclosure | claimed MEDIUM (alignment -2) | confidence 5/10
**Owner:** top committer: Devrandom (8/8 recent commits on metrics.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** mitigated, votes {TP 1, FP 0, CV 0}
**Preconditions (2):** cluster-internal network reach to :4010; no auth/firewall restriction in repo.
**Threat-model match:** T8 (internal topology disclosure; owner risk-accepted internal-only)
**Why:** /metrics on a separate :4010 listener (main.go:154-158) leaks backend URLs and model names as label values plus live gauges (metrics.go:51-93), with no auth. Owner risk-accepts internal-only disclosure (THREAT_MODEL §5). Derived LOW; claimed MEDIUM slightly inflated. Residual action: firewall :4010 to observability hosts and confirm nginx never proxies /metrics.
**Reachability evidence:** metrics.go:51-93 → main.go:154-158 (:4010)

### [LOW] Passthrough mode routes with an empty model, so per-model ACL is never enforced  (f008)
`handler.go:222` | auth-bypass | claimed LOW (alignment 0) | confidence 4/10
**Owner:** top committer: Devrandom (17/17 recent commits on handler.go, 8/8 on router.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** exploitable, votes {TP 1, FP 0, CV 0}
**Preconditions (2):** deployment runs in passthrough (single Backend.URL); an ACL is configured (wired even in passthrough at main.go:124).
**Threat-model match:** none
**Why:** `PassthroughRouter.Route` returns Model="" (router.go:95-97) so `acl.Check("",...)` finds no rule and allows (acl.go:73-75); the operator ACL is silently ineffective in passthrough mode. Config-dependent and operator-shaped → LOW. Fix: fail fast if ACL is set in passthrough, or parse the body model and enforce it.
**Reachability evidence:** router.go:95-97 → handler.go:225 → acl.go:73-75; main.go:124 (ACL wired)

### [LOW] Broad /v1/* path forwarding and blocklist header pass-through  (f010)
`handler.go:318` | header-injection | claimed LOW (alignment 0) | confidence 3.5/10
**Owner:** top committer: Devrandom (17/17 recent commits on handler.go); no CODEOWNERS entry; component: local-ai-proxy/
**Verdict:** needs_manual_test, votes {TP 1, FP 0, CV 0}
**Preconditions (2):** authenticated external client reaching /v1/*; backend interprets a forwarded header (e.g. X-Forwarded-For) or forwarded /v1/* subpath in a security-relevant way.
**Threat-model match:** T10 (broad pass-through to backend internals)
**Why:** Any /v1/* path is forwarded verbatim and all client headers except hop-by-hop are copied (handler.go:308-318, 685-692); only X-User-Id/X-User-Groups are stripped (trusted.go:21-28), so X-Forwarded-For passes through untouched. Real, but impact is backend-dependent, so runtime verification is required. Fix: explicit header/path allowlist.
**Reachability evidence:** handler.go:308-310 → 318 → 685-692; trusted.go:21-28
> Recommend a human build a PoC; static reasoning hit its limit.

## Dropped

| id | title | file:line | why dropped |
|---|---|---|---|
| f005 | etcd client silently falls back to plaintext when TLS env vars are unset | etcdclient.go:63 | false_positive — exclusion rule 8 (trusted operator env) / not_actionable: config/hardening gap driven by operator env provisioning, no concrete exploit |
| f007 | /healthz echoes backend URLs, probe error strings, and model names | handler.go:449 | false_positive — already_handled: main listener loopback-bound, reachable only locally; owner risk-accepts internal-only |
| f009 | CLI-discovered model IDs written as etcd keys after only weak validation | cli.go:203 | false_positive — exclusion rule 8 (trusted operator CLI) / not_actionable: low impact, no security boundary crossed |
