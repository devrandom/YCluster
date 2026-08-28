# Mitigate DoS on the authentik forward-auth / proxysession write path

Forward-auth (embedded outpost behind nginx `auth_request`) writes a
`proxysession` DB row on **every unauthenticated request** that isn't
allowlisted. In `mode_forward.go` `forwardHandleNginx`, the only
short-circuits are valid claims (200) or an allowlisted path; anything else
falls through to `s.Save(...)` and returns 401. With no cookie each hit
generates a fresh session key, so the observable behaviour is **one INSERT
(plus one SELECT) per hit** against Postgres.

Two aggravating facts make this trivially exploitable as a throughput-DoS:

- The outpost itself has **no rate limiting / throttling** in the
  forward-auth path (`proxyv2/...`).
- The publicly reachable `location /` roots that run this forward-auth gate
  currently have **no `limit_req`** — nginx rate limits only exist on a few
  admin-api / WG / status endpoints, not on the gated roots.

Consequence: anyone (or an aggressive bot/crawler) can hammer a gated vhost
at arbitrary rate and force that many **fsync'd proxysession INSERTs/s** onto
the slow RBD/dm-crypt sync-write volume, each later followed by a DELETE and
autovacuum/checkpoint churn. Rows expire quickly, so it isn't unbounded
storage — it is unbounded **write amplification on the exact path that is
already a latency bottleneck** behind the forward-auth perf work.

This is already happening at baseline by accident: the internal Blackbox
monitor probes those same gated endpoints unauthenticated, and the forward-auth
perf investigation (private incident doc) recorded millions of proxysession
INSERT/DELETE cycles with autovacuum cycling every few minutes. So the "low
traffic" assumption does **not** hold for this table.

## Tasks

1. **HTTP rate limiting (boundary).** Add a `limit_req` zone keyed by client
   IP on the gated `location /` roots that run the outpost forward-auth, with
   a sane sustained rate + burst, and map overflow to 429 / the login
   redirect. Cover both the plain and tunnel listeners.
2. **Stop baseline insert churn.** Change Blackbox to probe allowlisted or
   health endpoints rather than unauthenticated gated roots, so each probe no
   longer writes a proxysession row. Measure insert rate before/after.
3. **Shrink the per-hit cost.** Tune proxysession expiry/cleanup so
   redirect-only rows are reused or dropped sooner; confirm autovacuum cadence
   stays sane under sustained load.
4. **Remove the write on unauthenticated hits (upstream).** Longer term:
   short-circuit an unauthenticated forward-auth with an in-memory redirect
   key instead of a DB INSERT per request (upstream authentik behavior —
   local patch or config where supported).

## Verification

- After 1+2: proxysession INSERT rate should drop to ~1/s baseline (only real
  auth transitions) and stay flat under a synthetic load, with no latency
  regression on surviving traffic.
- Re-run the synthetic-cookie stress and confirm no proxysession slow/lock
  statement appears in the PG log.
