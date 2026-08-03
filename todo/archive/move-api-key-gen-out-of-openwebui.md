# Move API key generation out of Open WebUI

Per-user `sk-…` API keys are currently minted inside Open WebUI's UI
(Account Settings → API Key → Generate) and stored plaintext in the
OWUI `api_key` postgres table. The cluster auth validator
(`local-ai-proxy-auth.service`) reads them on every request via a SQL
join against OWUI's `user`/`group_member`/`group` tables.

This makes OWUI the source of truth for an identity-bearing secret that
authenticates against the **inference gateway**, which OWUI itself is
just one client of. The master key already lives in etcd (cluster-owned,
cluster-tool-managed); per-user keys should follow the same shape.

## Why now

- **Plaintext at rest** (paired with
  [`todo/architecture/a2-api-keys-plaintext.md`](../architecture/a2-api-keys-plaintext.md)).
  Even after we move keys to cluster storage, OWUI's plaintext copies
  still exist until that path is shut off.
- **OWUI dependency on the auth hot path.** If OWUI is being upgraded,
  migrated, or briefly unreachable, every per-user auth call fails —
  the gateway should validate keys without touching the OWUI database.
- **Identity is OWUI-shaped today.** The validator emits
  `X-User-Id = u.email` and `X-User-Groups = OWUI group names`. The
  gateway's ACL (`user:<email>`, `group:<name>`) is tied to that shape.
  Moving keys out is the moment to decide whose identity this is.
- **No rotation flow** (paired with
  [`todo/security/secrets-rotation.md`](../security/secrets-rotation.md)).
  Today there is no documented procedure for revoking a leaked
  per-user key other than logging into OWUI and deleting the row.
- **No CLI.** Per-user key lifecycle is hidden in a web UI; ops and
  automation can't see or touch it.

## Goals

- Per-user API keys are minted by a cluster tool (`ycluster` CLI), not
  the OWUI web UI.
- Key + identity data lives in cluster storage (etcd), read directly by
  the auth validator — no SQL hop into OWUI.
- Hot-reload on add/revoke, same shape as the model store (etcd watch).
- Hash-at-rest for stored keys (resolves A2 for the new path).
- Rotation flow documented and exercisable from the CLI.
- OWUI continues to work during a migration window, then OWUI's
  per-user key generation is locked down (the OWUI admin panel setting
  to disable user key creation, or equivalent).

## Open design questions (resolve first)

These block any concrete work; each is small but the answers pin
down the schema and the OWUI integration story.

1. **Whose identity is this?** The validator currently emits the
   OWUI email as `X-User-Id`. Three options:
   - (a) Keep OWUI as identity, just move keys+groups out — OWUI
     `user.email` still authoritative, ACL entries stay `user:alice@…`.
   - (b) Introduce a cluster `user` namespace
     (`/cluster/users/<name>`) and treat OWUI users as a synced
     projection. ACL entries become `user:<short-name>`.
   - (c) Decouple entirely: keys carry a synthetic `user_id` (uuid)
     and the email is metadata. ACL entries use the synthetic id.
   (a) is the smallest change; (b) is the right answer if we want
   OWUI to be replacable; (c) is the cleanest but breaks every
   existing ACL config. Pick one before sketching the etcd schema.

2. **Etcd schema.** Two prefixes look reasonable — pick one and
   stick with it:
   - `/cluster/config/inference/keys/<key-id>` — value carries the
     hash, owner, groups, created_at, label. Keeps inference config
     co-located but overloads the prefix semantically.
   - `/cluster/users/<user>/keys/<key-id>` — same payload. Aligns
     with (b) above; ACL entries can be derived from the same prefix.
   Hash function: argon2id with per-key salt (slow on purpose —
   auth-validator validates hashed vs presented key via re-hash,
   not a dict lookup). The secret only ever appears at creation time
   in the CLI's stdout.

3. **OWUI integration during transition.** OWUI itself calls the
   inference gateway as a backend (loopback). When a user pastes
   their `sk-…` into OWUI's Connections page, OWUI forwards it on
   every request. Two ways to keep OWUI working:
   - **Dual-read** in the validator: check etcd first, fall back to
     OWUI postgres until the migration window closes. Simplest
     rollback story.
   - **OWUI key-rotation hint** in the CLI: `ycluster inference key
     create --bind-to-openwebui-user <email>` returns the key once
     and prints the OWUI paste-instruction once. No code change in
     OWUI; user does the paste. Works immediately, breaks if user
     forgets to update OWUI after a revoke.

4. **CLI surface.** Three reasonable shapes:
   - `ycluster inference key create|list|revoke …` — sits next to
     `ycluster inference add/remove`. Cheap, but conflates model
     routing with identity.
   - `ycluster api-key …` — top-level. Treats the key as a
     first-class cluster object. Pairs naturally with future
     `ycluster user …` if we go with option (b).
   - `ycluster user key …` — assumes identity namespace exists
     (option b).
   Pick one early; the rest of the work assumes it.

5. **Locking down OWUI.** OWUI has an env flag
   (`ENABLE_API_KEYS` or admin-panel toggle) to prevent users from
   minting their own keys. Decide whether to flip it at install time
   (locked from day one) or after the migration window (most
   user-friendly). Recommend: locked from install; OWUI keeps
   accepting pasted keys so users can rotate to cluster-issued
   ones.

## Subtasks (ordered)

1. **Resolve the open questions above.** One short doc
   (`docs/design/api-key-ownership.md`) capturing the picked answers
   + rationale, before any code.
2. **Etcd schema + write/read helpers** in
   `config/ansible/admin/files/ycluster/ycluster/common/etcd_utils.py`
   (or a new `apikeys.py`): `create_key(owner, groups, label)`,
   `list_keys()`, `revoke_key(id)`, `validate_key(presented)` —
   argon2id hash, deterministic per-key salt, constant-time compare.
3. **CLI subcommand** per the chosen surface (see open question 4).
   Must print the plaintext `sk-…` once at creation and never again.
   `list` shows id/owner/created_at/label, never the secret.
   `revoke` is idempotent.
4. **Auth validator rewrite** (`local-ai-proxy-auth.py`): read keys
   from etcd, hot-reload on the keys prefix via the existing
   `etcd_utils.get_etcd_client()` watcher pattern. Replace the
   psycopg2 dependency with the etcd source. Preserve the master-key
   path unchanged.
5. **Dual-read window** (if open question 3 picked dual-read): keep
   the OWUI postgres lookup as a fallback, gated on a config flag
   (`AUTH_ALLOW_OWUI_FALLBACK=true`), default off, documented removal
   date. The flag lives in `/etc/local-ai-proxy/config.yaml` so it
   can be flipped by a playbook without redeploy.
6. **OWUI admin-panel lockdown.** Playbook edit
   (`install-open-webui.yml`): set the env flag that disables
   per-user key generation in OWUI's UI. Verify the OWUI Settings
   page reflects it.
7. **Migration runbook** (`docs/operations/inference-api-keys.md`):
   - List existing OWUI keys + their owners.
   - For each, `ycluster inference key create --owner <email>` (or
     chosen equivalent), print the new key once.
   - Operator pastes the new key into OWUI's Connections page for
     that user; user updates their `OPENAI_API_KEY` env var.
   - Once every row is migrated, flip the dual-read flag off and
     prune OWUI's `api_key` table.
8. **system-test additions** (`dev/cluster/system-test.sh`, in the
   `sec_inference` section): create a key via the CLI, auth with it,
   revoke it, confirm 401, confirm the master key still works,
   confirm OWUI-fallback (when enabled) still works.
9. **Hash-at-rest for the new path** — bundled with this work, not
   punted to A2. Argon2id is the answer; document the parameters in
   the runbook so a future bump is mechanical.
10. **Deprecate the OWUI key path in `docs/operations/inference.md`**
    and `docs/usage/inference.md`: replace the OWUI screenshot-style
    instruction with the CLI one. Note the OWUI-fallback window in
    operations/inference.md; remove from usage/inference.md once the
    window closes.
11. **Secrets-rotation pairing.** Once the CLI exists, write the
    per-user rotation flow (`docs/operations/secrets-rotation.md` or
    fold into the api-keys runbook): revoke → create → paste →
    revoke old. This unblocks the
    [`todo/security/secrets-rotation.md`](../security/secrets-rotation.md)
    stub for the per-user side.

## Dependencies / pairing

- [`todo/architecture/a2-api-keys-plaintext.md`](../architecture/a2-api-keys-plaintext.md)
  — fold hash-at-rest into this work rather than chasing the OWUI
  upstream change.
- [`todo/security/secrets-rotation.md`](../security/secrets-rotation.md)
  — per-user rotation is the bigger half of the rotation story;
  tackle together.
- [`todo/security/etcd-rbac-by-cert.md`](../security/etcd-rbac-by-cert.md)
  — once keys live in etcd, the etcd RBAC split has to keep the keys
  prefix readable by `local-ai-proxy` (or wherever the validator
  ends up running). Coordinate the prefix ownership.
- [`todo/inference/upstream-exo-prompt-logging.md`](upstream-exo-prompt-logging.md)
  — unrelated; just noting that "inference" folder is becoming the
  right home for gateway-side concerns.

## Acceptance

- `ycluster inference key create …` (or chosen surface) returns one
  `sk-…` value, never persisted in plaintext.
- Auth validator passes for the new key on the next request (no
  restart), rejects within one watch-tick after revoke.
- OWUI's web UI no longer offers a "Generate API Key" button to
  end users (env flag flipped in `install-open-webui.yml`).
- `docs/usage/inference.md` no longer tells users to go to OWUI for
  a key.
- system-test exercises create → use → revoke → 401 without
  touching OWUI.
- Existing OWUI-issued keys still work during the dual-read window,
  and stop working once the flag is flipped off.

## Out of scope

- Replacing OWUI entirely (option b's "OWUI is replacable" goal).
- mTLS / per-request signing.
- Per-key rate limits / quotas (separate item if it comes up).

## Status

DONE. Per-user gateway keys are now minted and hashed in etcd
(`/cluster/users/<email>/keys/`, argon2id) and validated by
`local-ai-proxy-auth` with no OWUI postgres hop; the CLI
(`ycluster inference key create|list|revoke|migrate`) is in
`config/ansible/admin/files/ycluster/`, migration ran on prod and
copied existing OWUI keys into the new schema (tokens keep working,
no rotation), docs were updated (`docs/design/api-key-ownership.md`,
`docs/operations/inference.md`, `docs/usage/inference.md`), and the
dev-cluster system-test `sec_inference` section covers
create → auth → revoke → 401 → master key.

Deliberately not done:
- The OWUI admin-panel lockdown (subtask 6) was dropped by decision —
  OWUI keeps its own per-user keys for signing into OWUI; OWUI-key
  generation is intentionally left enabled and is no longer coupled to
  the gateway auth path.
- The dual-read window (subtask 5) was implemented then removed by
  decision — the validator is etcd-only.
- Secrets-rotation flow (subtask 11) is folded into
  `todo/security/secrets-rotation.md`, still open; gateway keys are
  revocable today and the OWUI-side copy means no forced rotation.
