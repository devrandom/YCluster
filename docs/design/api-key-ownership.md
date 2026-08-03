# API key ownership (moving inference auth off Open-WebUI)

## Status

**Draft — design decided, nothing implemented.** This resolves the
open design questions in
[`todo/inference/move-api-key-gen-out-of-openwebui.md`](../../todo/inference/move-api-key-gen-out-of-openwebui.md)
so the implementation subtasks can proceed. Rotation and etcd RBAC are
explicitly out of scope here (see "Out of scope").

Decided so far:

- The coupling to remove is **only** between Open-WebUI's key storage
  and the inference gateway's auth validator. The validator stops
  reading OWUI's `api_key` table on the auth hot path and reads etcd
  instead.
- OWUI keys **remain OWUI's own mechanism for signing in to OWUI**.
  We do not touch `ENABLE_API_KEYS`, and we do not replace OWUI's
  per-user key flow for OWUI access.
- Key + identity data for the gateway lives in etcd under a new
  `/cluster/users/` namespace, read directly by the auth validator.
  Etcd is the system of record for **gateway** auth; OWUI stays the
  system of record for **OWUI** auth.
- New gateway keys are minted by a cluster tool (`ycluster inference
  key …`).
- Existing OWUI keys are **copied into the new schema during
  migration**, so users keep their exact `sk-…` value — no rotation.
- Identity is **email-first, cluster-shaped**: gateway keys are owned
  by a cluster `user` object keyed by email, and the validator emits
  `X-User-Id = <email>` to preserve every existing ACL entry. The
  cluster `user` namespace is forward-compatible with demoting email
  to a profile field later (see `docs/design/user-rename.md`).
- Gateway keys are stored **hashed** (argon2id, per-key salt).
  Existing keys are re-hashed as part of the copy, so nothing is
  plaintext in etcd.

## Motivation

Per-user inference auth is currently coupled to Open-WebUI in two
ways:

1. **Minting** happens inside the OWUI web UI (Account Settings →
   API Key → Generate). Ops and automation have no CLI surface, and
   there is no path to inspect or revoke keys outside a browser.
2. **Validation** reads the secret back out of OWUI's `api_key`
   postgres table on every request. `local-ai-proxy-auth.service`
   joins `api_key → user → group_member → group` to produce
   `X-User-Id` / `X-User-Groups`, which the gateway's ACL
   (`user:<email>`, `group:<name>`) trusts.

The important coupling is **#2**: OWUI's database sits on the
inference auth hot path. An OWUI upgrade, migration, or outage takes
per-user inference auth down with it, and it leaves the gateway's
secrets plaintext at rest (paired with
[`todo/architecture/a2-api-keys-plaintext.md`](../../todo/architecture/a2-api-keys-plaintext.md)).

The fix is to make the gateway validate keys from cluster storage,
matching the shape already used for the admin master key
(`/cluster/config/inference/master-key`). OWUI keeps using its own
keys to gate access to OWUI itself — that is OWUI's business and is
unchanged. We are decoupling the *gateway's source of truth*, not
OWUI's login/key flow.

## Current behavior (baseline)

- **Master key** — etcd
  `/cluster/config/inference/master-key`; validated by constant-time
  compare in `local-ai-proxy-auth.py`; emits `X-User-Id: root`,
  `X-User-Groups: ""`.
- **Per-user keys** — OWUI `api_key` table; validator emits
  `X-User-Id: u.email` and `X-User-Groups: <group names>` (an OWUI
  `admin` role is appended as the `admin` group).
- **Request path** — nginx `auth_request` → `local-ai-proxy-auth` →
  sets `X-User-Id`/`X-User-Groups` from loopback → the proxy trusts
  them. The proxy itself never validates per-user identity.
- **OWUI → gateway** — OWUI calls the gateway as a loopback backend;
  when a user pastes their `sk-…` into OWUI's Connections page, OWUI
  forwards it as `Authorization: Bearer` on every request.

## Decided design

### 1. System-of-record boundary

Two separate concerns, two systems of record:

- **OWUI access** — Open-WebUI's own login and per-user API key flow
  stay exactly as they are. `ENABLE_API_KEYS` is left alone (it is
  `True` today and stays `True`). OWUI keys continue to control access
  to OWUI.
- **Gateway auth** — after a user is in OWUI (or uses a key directly
  against the gateway), the gateway validates per-user keys from etcd.
  OWUI's `api_key` table is read only during the one-time migration,
  never on the auth hot path.

The same `sk-…` value can still serve both roles (a user pastes their
gateway key into OWUI's Connections page so OWUI can talk to the
gateway on their behalf). That is fine — OWUI is just a client of the
gateway; the decoupling is in where the *validator* looks, not in the
secret value itself.

### 2. Identity: cluster `user` namespace, email-keyed for now

Adopt option (b) from the TODO: introduce a cluster `user`
namespace under `/cluster/users/`. Each user is currently keyed by
email (matching how OWUI identities the user today and matching the
existing `/cluster/users/<email>` user record, a JSON blob that
already holds `ssh_keys`). Email remains
the value of `X-User-Id`, so **no existing ACL entry changes.**

Rationale for (b) over the alternatives:

- **(a) keep OWUI as identity, just move keys out** is the smallest
  change but would leave OWUI rows as the canonical user list for the
  gateway, which is the coupling we are removing.
- **(c) synthetic UUID identity** is the cleanest long-term but
  breaks every existing `user:<email>` / `group:<name>` ACL entry and
  forces a cluster-wide identity migration now.
- **(b)** gives us a real cluster-owned identity object immediately,
  makes OWUI replaceable as far as the gateway is concerned, and
  keeps the email-based ACL working today. It lines up with the
  long-term direction in `docs/design/user-rename.md` (email demoted
  to a profile field, authentik UUID as the stable ID) without
  committing to it now — the namespace is stable enough to re-key
  later.

Groups are written to the `/cluster/users/<email>/groups` child key
so the validator no longer needs OWUI's `group_member`/`group` tables.
The `admin` group is treated like any other group value for per-user
keys (an `admin` entry grants admin ACL semantics through the existing
`group:admin` machinery). This adds a new child key and does not touch
the existing SSH-key blob at `/cluster/users/<email>`.

### 3. Etcd schema

```
/cluster/users/<email>               -> {"ssh_keys": [...]}           # existing, UNCHANGED
/cluster/users/<email>/groups        -> ["group-a", "admin"]          # new child key
/cluster/users/<email>/keys/<key-id> -> {"hash": ..., "created_at": ..., "label": ...}
```

The existing user record at `/cluster/users/<email>` (a single JSON
blob holding `ssh_keys`) is **left untouched** — we do not migrate it
to the nested style nor add fields to it. The new data is a set of
sibling child keys under the same user prefix:

- `/cluster/users/<email>/groups` — JSON array of group names. This
  is where the validator reads `X-User-Groups` from, replacing the
  OWUI `group_member`/`group` join. (An `admin` entry here grants
  admin ACL semantics through the existing `group:admin` machinery.)
- `/cluster/users/<email>/keys/<key-id>` — one record per active
  key, holding `hash`, `created_at`, and an optional `label`.

Keeping the API keys as child keys under `/cluster/users/<email>/`
(etcd happily stores a value on the user key and child keys beneath
it) co-locates identity and its secrets while leaving the existing
SSH-key blob alone.

The `<key-id>` is a short random ulid/uuid — it is the reference used
by `list`/`revoke` and is **not** the secret. The `sk-…` value itself
exists only transiently: at CLI creation time (shown once in stdout)
or during the one-time migration copy (read from OWUI, re-hashed,
discarded). The stored `hash` is what the validator checks against a
presented token.

Placing keys under `/cluster/users/` (rather than
`/cluster/config/inference/keys/`) keeps identity and its secrets
co-located and is the natural home for a later `ycluster user …`
surface. The inference model store stays where it is
(`/cluster/config/inference/models/`).

Hash choice: **argon2id** with a per-key random salt. The validator
re-hashes the presented token with the stored salt and compares —
this is intentionally not a lookup-by-plaintext, is password-hard on
purpose, and handles variable salt/params per record. This resolves
`a2-api-keys-plaintext.md` for gateway keys.

### 4. CLI surface

`ycluster inference key …`:

```
ycluster inference key create --owner <email> [--group <name> ...] [--label <text>]
ycluster inference key list [--owner <email>]
ycluster inference key revoke <key-id>
ycluster inference key migrate            # one-time: copy OWUI keys into etcd
```

Sits next to `ycluster inference add/remove`, which is where ops
already go for gateway concerns, and avoids inventing a top-level
`user` command before the namespace is proven out.

Semantics:

- `create` prints the plaintext `sk-…` **once** and never persists or
  reprints it. It writes the key to `/cluster/users/<email>/keys/`
  and writes the `groups` from the flags to
  `/cluster/users/<email>/groups` if present. Storing groups here
  replaces the OWUI `group_member` join; the existing SSH-key blob at
  `/cluster/users/<email>` is left alone.
- `migrate` reads the existing OWUI `api_key` rows (plaintext), and
  for each row hashes the key into `/cluster/users/<email>/keys/`
  with the owner's groups, so the **same `sk-…` keeps working** — no
  re-issue, no rotation. The plaintext is never written to etcd and
  is discarded after hashing.
- `list` shows `key-id`, owner, label, created_at — never the secret
  and never the hash.
- `revoke` is idempotent (removing an already-absent key is a no-op
  success).

No `ycluster user …` command yet; the namespace is written by the
key tooling for now.

### 5. Key delivery (how a new key reaches the user)

Existing keys need no delivery (migration copies the value, users keep
their `sk-…`). New keys — created via the CLI or self-service — use a
**mint-once, present-once** rule: the plaintext exists exactly once and
is never retrievable afterward.

Two presenters:

- **Self-service (end users).** A small "API keys" surface on the
  existing admin `app.<domain>` forward-auth vhost (`app.py` behind
  the authentik outpost). The user is already logged in via
  `X-Authentik-UID` / `X-Authentik-Email`, so the route reads/writes
  only under `/cluster/users/<email>/`. On create the backend mints
  the `sk-…`, stores only the argon2id hash in etcd, and returns the
  plaintext **once** in the HTTP response. The UI shows it in a
  single-use box with a copy button
  (`navigator.clipboard.writeText`) and an explicit "I've saved it"
  acknowledgement.
- **Operator / bootstrap (admin, brand-new users not yet in
  authentik).** The CLI `create` prints the `sk-…` to stdout once and
  the operator relays it to the user out-of-band. Same mint-once
  rule, but the operator is the human in the loop.

The invariant that makes "plaintext only once" enforceable rather
than a convention: **there is no re-display path.** `list` returns
only id/label/created_at, never the secret; there is deliberately no
`GET …/keys/<id>/value` endpoint and no `--show` flag. If a user
misses the one-shot value (closed the tab, lost the message), the
recovery is revoke + create, not re-fetch.

Presentation mechanics:

- The value is copied via the clipboard API, not rendered into a form
  field's `value` (avoids autofill, browser history, and server-side
  page source).
- The one-shot UI is a distinct confirmation step; nothing shows the
  plaintext on page reload, and refresh after create shows only the
  key's metadata.
- The master-key / admin path is unchanged (operator-created, not
  surfaced to end users).

### 6. Auth validator rewrite

`local-ai-proxy-auth.py`:

1. **Master key path unchanged** — etcd
   `/cluster/config/inference/master-key`, constant-time compare.
2. **Per-user path** reads `/cluster/users/` from etcd directly:
   find the user whose key-record matches the presented token
   (iterate records, verify each stored hash against the token via
   argon2), then emit `X-User-Id: <email>` and
   `X-User-Groups: <groups from the user record>`.
3. **Hot-reload** on the `/cluster/users/` prefix using the existing
   `etcd_utils.get_etcd_client()` watcher pattern, mirroring how the
   model store reloads. This replaces the `psycopg2` dependency and
   the OWUI SQL join; `psycopg2` is dropped once the migration
   completes.

The validator keeps emitting `X-User-Id: <email>` so the proxy's ACL
and all existing config keep working unchanged.

### 7. Dual-read / migration window

New config flag in `/etc/local-ai-proxy/config.yaml`
(`auth.allow_owui_fallback`, default **false** for fresh installs).
When true, the validator checks etcd first and, on a miss, falls back
to the existing OWUI postgres lookup. This lets an existing cluster
roll out the new validator while keys are being copied over.

- **Fresh installs**: flag off — the validator never touches OWUI.
- **Existing cluster**: playbook flips it on during the migration,
  operators run `ycluster inference key migrate`, then flip it off.
  OWUI's `api_key` table can then be pruned (its data has been
  re-hashed into etcd).

The flag lives in the Ansible-managed YAML so flipping it is a
playbook change, not a redeploy.

## Migration procedure (existing cluster)

Written up as a runbook in `docs/operations/inference.md` / a short
`docs/operations/inference-api-keys.md`; the shape:

1. Enable `auth.allow_owui_fallback=true` and deploy the new
   validator.
2. Run `ycluster inference key migrate`. For each OWUI key, this
   hashes the existing `sk-…` into `/cluster/users/<email>/keys/`
   with the owner's groups.
3. Because the value is unchanged, users keep using their **same**
   key — no paste-back into OWUI, no `OPENAI_API_KEY` change.
   (If a user was already pasting it into OWUI's Connections page,
   that continues to work.)
4. When no validator request is hitting the OWUI fallback, flip the
   flag off, then prune the `api_key` table.

## Implementation notes

- etcd helpers in
  `config/ansible/admin/files/ycluster/ycluster/common/` (a new
  `apikeys.py`): `create_key`, `list_keys`, `revoke_key`, `migrate_owui_keys`,
  `validate_key`, using the existing TLS-aware `get_etcd_client()`.
  `migrate_owui_keys` needs read access to the OWUI `api_key` /
  `user` / `group_member` / `group` tables (same query the validator
  uses today) but only during migration.
- Self-service delivery surface: a new route in
  `config/ansible/admin/files/app.py` (and nginx location reusing the
  existing `admin-api-authentik-outpost.conf` forward-auth directives),
  both reading and writing via the same `apikeys.py` helpers as the
  CLI so mint/present-once semantics are shared. The route resolves
  the user from `X-Authentik-UID` / `X-Authentik-Email` and is scoped
  to `/cluster/users/<email>/`; no endpoint exposes a stored `value`.
- system-test additions in `dev/cluster/system-test.sh`
  (`sec_inference` section): create → auth → revoke → 401, master key
  still works, OWUI-fallback still works when enabled, and a
  `migrate` copy of a pre-seeded OWUI key authenticates without a
  rotation.
- `docs/operations/inference.md` "Per-User API Keys" section is
  rewritten to the CLI; the OWUI screenshot-style instructions are
  removed for the gateway path.
- `ENABLE_API_KEYS` in
  `config/ansible/app/files/docker-compose.yaml` is **unchanged** —
  OWUI keys keep controlling access to OWUI.

## Out of scope

- **Replacing Open-WebUI's own login/key flow** — OWUI keys remain
  OWUI's mechanism for OWUI access; only the gateway's source of
  truth moves to etcd.
- **Rotation flow** — see
  [`todo/security/secrets-rotation.md`](../../todo/security/secrets-rotation.md).
  The CLI's `create`/`revoke` primitives are the basis for a future
  rotation runbook, but no rotation procedure is defined here (and
  migration deliberately avoids rotation by re-hashing existing keys).
- **etcd RBAC** — see
  [`todo/security/etcd-rbac-by-cert.md`](../../todo/security/etcd-rbac-by-cert.md).
  Once keys live under `/cluster/users/`, that effort must keep the
  prefix readable by `local-ai-proxy-auth`; prefix ownership is
  coordinated there, not here.
- **mTLS / per-request signing**, and per-key rate limits / quotas.
