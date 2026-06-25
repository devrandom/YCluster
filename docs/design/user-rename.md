# Renaming user emails

## Status

**Draft — direction decided, nothing implemented.** Phase 1 (name editing)
shipped on the Users admin page; email editing deferred because the
fan-out is large enough to be its own design.

Decided so far:

- The Users admin page Edit modal is **name-only**. Email is not exposed.
- A proper rename is a `ycluster user rename <old> <new>` command, not a
  form field — the migration span is too wide for a modal.
- Email remains the cluster-wide identity key. Renaming does not change
  that — it migrates the key in place across every store that indexes
  by it.
- A longer-term direction exists to **demote email to a profile field
  and use the authentik user `pk` (UUID) as the cross-subsystem ID**.
  This is documented under "Long-term direction" below and is *not*
  on the immediate roadmap; Phase 2 is the bridge to it.

## Motivation

The Users admin page exposes per-row actions for an account:
password-reset link, grant/revoke admin, edit name, disable/enable.
The first three are local in scope; **changing the email is not** —
it's a coordinated rewrite across at least three systems that
independently index by email:

- **Authentik** — `username` and `email` columns, `username` is the
  natural key.
- **Open-WebUI** — `auth.email`, `user.email`, and the
  `user.oauth[provider].sub` JSON field are all `email`-typed. OWUI's
  primary key is its own UUID (`str(uuid.uuid4())` in
  `models/auths.py:97`), so chats and `api_key` rows can survive — but
  only if the rename reaches them.
- **etcd** — `/cluster/vms/<name>.owner`,
  `/cluster/users/<email>/ssh-keys`, and the usage_stats/model_usage
  Postgres rows all carry the email as a string key.

If the IdP email changes but nothing else does, the OWUI ↔ authentik
link breaks on the next login (no `(provider, sub)` match, no email
fallback either since the OWUI row still has the old email), and OWUI
creates a brand-new account with a fresh UUID. The old OWUI account —
chats, api_keys, settings — is orphaned, and `local-ai-proxy-auth`
keeps handing out `X-User-Id: <old-email>` for any legacy `sk-…` token
that resolves against it. ACL/attribution downstream is stale.

## Background: how the link works today

OWUI's OIDC callback (`backend/open_webui/utils/oauth.py:1645`) looks
up the user in this order:

1. By `(provider, sub)` in `user.oauth` JSON.
2. If not found, **and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=True`** (already
   set by `open-webui-manager:120`), fall back to `get_user_by_email`.
3. If still not found and `ENABLE_OAUTH_SIGNUP`, create a new user
   (fresh UUID).

The OIDC `sub` is the email because `sub_mode: user_email` is set on
the open-webui OAuth2 provider (`authentik-manager:68`).

OWUI ships an `OAUTH_UPDATE_EMAIL_ON_LOGIN` config knob (default
`False`; `backend/open_webui/config.py:3771`) that, when on, calls
`Auths.update_email_by_id` (`models/auths.py:185`) after a successful
login if the IdP-supplied email differs from the OWUI row. That
function updates both `auth.email` and `user.email` in place — UUID
preserved, `api_key` FK still valid, `local-ai-proxy-auth`'s
`X-User-Id` output follows the new email naturally on the next
authenticated request. The `user.oauth[provider].sub` field is *not*
updated by this code path, but the next login fixes it via the
merge-by-email fallback (`utils/oauth.py:1653`).

`local-ai-proxy-auth` is email-typed: `X-User-Id = user.email`
(`local-ai-proxy-auth.py:130`). Forward-auth on the admin pages is
also email-typed: `X-Authentik-Email` carries the email set by the
authentik outpost (`app.py:2650`). Both self-heal if the underlying
email column updates — they don't need to be rewritten by name.

## Long-term direction: a shared stable user ID

The root issue across all the migrations above is that **email is the
join key between systems that didn't agree to use it as a join key**.
Each system happens to store the email because users have one, and
that becomes the de-facto primary ID. Phase 2 papers over this by
making renames explicit and best-effort, but it doesn't fix the
underlying fragility — a future subsystem that needs to attribute
state to a user (e.g., new metrics, new service) will be tempted to
key by email again, and the migration will keep growing.

The alternative direction: introduce **a single stable user ID across
the cluster**, with email demoted to a profile field like display
name. The natural canonical ID is the **authentik user `pk` (UUID)**
— it's already allocated by the system of record, OWUI already
stores it inside its `oauth` JSON, and the OIDC `sub` claim can carry
it instead of the email.

### What changes

- **Authentik blueprint** (`authentik-manager:68`): switch
  `sub_mode: user_email` to `sub_mode: user_id` on the open-webui
  OAuth2 provider. New OIDC tokens carry the authentik UUID as `sub`;
  existing tokens (issued under the old setting) keep working until
  expiry.
- **OWUI `user.oauth[provider].sub`**: re-keyed from the old email to
  the authentik UUID. On every login, `get_user_by_oauth_sub` keeps
  matching because the IdP and OWUI now agree on the same `sub`. The
  email-fallback path (`OAUTH_MERGE_ACCOUNTS_BY_EMAIL`) becomes a
  *one-time* migration aid — turned off after the cutover, since the
  primary lookup is now stable.
- **`local-ai-proxy-auth`**: the SQL already joins `api_key → user`
  via `user_id` (the OWUI UUID) and returns `X-User-Id = user.email`.
  Switch the header to a stable ID: emit
  `X-User-Id: <authentik-uuid>` (resolved via the `oauth` JSON on the
  OWUI user) and keep `X-User-Email` for display. The Postgres tables
  that currently carry email as `user_id` (e.g., `model_usage`) gain
  a stable-ID column and migrate over.
- **etcd records**: `/cluster/vms/<name>.owner` becomes
  `/cluster/vms/<name>.owner_id`, and a sibling
  `/cluster/vms/<name>.owner_email` (or just `owner_email` inside the
  same record) carries the email for human display. SSH-keys subtree
  moves from `/cluster/users/<email>/...` to
  `/cluster/users/<user-id>/...` — email is a display hint on each
  record, not part of the path.
- **Forward-auth contract**: nginx `auth_request` can pass through
  both `X-Authentik-Email` (unchanged, for display) and a new
  `X-Authentik-User-Id` set by the embedded outpost from the user
  pk. Existing consumers reading `X-Authentik-Email` keep working;
  new consumers read the ID.

### Migration shape

A single cutover step (`ycluster user rekey`) rewrites every store in
one pass:

1. Authentik: flip `sub_mode` to `user_id`; snapshot all
   `(email → pk)` mappings.
2. OWUI: for each existing user, look up the matching authentik pk
   (by current email match), write `user.oauth[provider].sub =
   <authentik-pk>`. Drop the `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` setting
   from the compose env after the rewrite.
3. usage_stats DB: backfill a new `user_uuid` column on every
   `user_id`-bearing table (snapshot via OWUI ↔ authentik mapping).
4. etcd: rewrite every `/cluster/vms/<name>.owner` and move SSH-keys
   subtrees under the new user-id keys. Keep the old email-valued
   records around (read-only, for compatibility during cutover) and
   remove once nothing references them.
5. Wait for old JWT/session tokens to expire (or force a logout
   broadcast), then deprecate the email-keyed reads in code.

The critical part is the order: **sub_mode must change in step 1
before step 2**, so that new OWUI logins land with the new `sub`
format. Old tokens in flight can still resolve during step 2 because
the `user.oauth` JSON is being rewritten in place — OWUI's primary
lookup is `(provider, sub)`, but the email-fallback covers the gap
until step 2 finishes.

### Why this is more than Phase 2 dressed up

Phase 2 treats email rename as an explicit operator action that
migrates every store at the moment of rename. This treats email
rename as a **non-event**: emails change freely, and no consumer
ever has to know. New subsystems default to the stable-ID convention
without thinking about email. It's the difference between
"email-as-join-key is allowed, here are the rules for migrating
when it changes" and "email is a profile field, the system has no
notion of a join-on-email."

### Cost and timing

Roughly an order of magnitude more work than Phase 2 — a coordinated
cutover touching authentik config, OWUI's OIDC handler contract,
the forward-auth header set, every etcd-keyed store, and the
Postgres analytics tables. Estimate **15–25 days** when scheduled,
plus a multi-week soak with the old `sub_mode` still accepted.

Recommendation: **ship Phase 2 first** (cheap, covers the actual
common case of "typo or domain migration"). Revisit this direction
the next time a new subsystem needs to attribute state to a user, or
when a second rename pain point appears (e.g., a user wanting to
merge two accounts) — at that point the long-term cost becomes
worth paying for.

## Phase 1 — name editing (shipped)

The Users admin page collapses all per-row actions behind a single
`…` menu. The Edit modal updates only the display name, which is
identifiably safe (no cross-system references). Backend:
`authentik_manager.update_user(email, name=...)` PATCHes
`/core/users/{pk}/`. No downstream is affected.

## Phase 2 — `ycluster user rename <old> <new>` (planned)

A single CLI command that performs a coordinated rename. The action
is **explicit** — the user types an old email and a new email — and
the operation is **best-effort with a clear migration report**, not a
silent automatic follow.

The shape of the migration:

1. **Validate**: both emails parse, new email is unused in
   authentik (return a list of conflicts otherwise and abort).
2. **Authentik rename**: PATCH `username` and `email` on
   `/core/users/{pk}/`. This is the trigger — the IdP now hands out
   the new email as `sub` and `X-Authentik-Email`.
3. **OWUI follow**: directly write the local OWUI DB
   (`UPDATE "user" SET email = …; UPDATE auth SET email = …; UPDATE
   "user" SET oauth = jsonb_set(...)`). Update by authentik email,
   match the row by `user.email = old`. This keeps the UUID and the
   `api_key` FK intact regardless of whether
   `OAUTH_UPDATE_EMAIL_ON_LOGIN` is enabled.
4. **etcd pass**:
   - `/cluster/vms/<name>.owner` — string rewrite.
   - `/cluster/users/<email>/...` — move the entire subtree (SSH
     keys) to the new key; the old key is removed.
5. **Postgres pass** (usage_stats DB):
   - `vm_events.owner`, `vm_samples.owner`, `model_usage.user_id`,
     `usage_stats.user_id` — string rewrite.
6. **Report**: list what was rewritten (counts per store) and what
   was left untouched (e.g., logs in Open-WebUI's `chat` table that
   quote the email in message content — explicitly out of scope).

Failure mode: if step 2 succeeds and a later step fails, the
remaining steps are idempotent enough to retry — except step 2,
which would re-PATCH the same new value (safe). A `--dry-run` flag
prints the plan without writes.

### Disabling itself

Disable-before-rename (`ycluster user rename` should refuse if the
old account is currently active and a `--force` flag isn't passed).
The order matters because OWUI's `OAUTH_MERGE_ACCOUNTS_BY_EMAIL`
fallback would otherwise happily merge a separate new-email account
into the old email's OWUI row during the gap.

Actually, this concern is theoretical as long as step 2 happens
before any new login. Worth documenting; not blocking.

### Edit modal later

Once the CLI exists and `OAUTH_UPDATE_EMAIL_ON_LOGIN=True` is
enabled by default in `open-webui-manager`, the Edit modal can grow
an Email field that issues the same rename — but as a confirmation
dialog, not a silent form submit, given the scope.

## Implementation order

Estimates are focused working days, including cluster testing,
Ansible idempotency, and doc updates.

1. Enable `OAUTH_UPDATE_EMAIL_ON_LOGIN=True` in
   `open-webui-manager` (lines 114–124). Pure config — the runtime
   code path already exists upstream. — **0.5 d**: verify on a dev
   cluster that a test email rename survives a relogin (UUID
   preserved, `api_key` still resolves, chats intact).
2. `ycluster user rename` command — phases 2, 3, and 5 above.
   — **2–3 d**: the authentik + OWUI + etcd writes are
   straightforward; the etcd pass needs care to be transactional
   enough that a crash mid-rewrite leaves a recoverable state.
3. Migration report and `--dry-run`. — **0.5 d**, folded into 2.
4. Update the Users admin Edit modal to surface email editing,
   behind a confirmation prompt that lists what will be rewritten.
   — **0.5–1 d**: thin wrapper around the CLI; the heavy lifting
   is already done.
5. Postgres usage_stats rewrite (`vm_events.owner`, `model_usage`,
   etc.) — best done as part of step 2 if usage_stats is on the
   same DB as OWUI, separately if not. The `local-ai-proxy-auth`
   SQL itself doesn't need to change — the per-row `email` update
   flows through automatically.

Total ≈ **3–5 days**.

## Open questions

- **Mutability policy**: should `ycluster user rename` be admin-only,
  or allowed on the user's own account? Own-account renames are
  rare and easy to mis-verify (the user can't log in until they
  follow the recovery link), so admin-only is probably right.
- **`OAUTH_UPDATE_EMAIL_ON_LOGIN` default**: leave it off and rely
  purely on the CLI, or turn it on by default? Turning it on
  eliminates the "OWUI row has stale email after a self-service
  rename" failure mode but adds an OWUI internal write to every
  login. Decide when we know how often users rename their emails.
- **Log/audit content**: chats and tool-call messages can embed the
  email as free text. Cleaning those up is out of scope for now,
  but document that the migration leaves such content unchanged.
- **Idempotency**: how do we handle the case where `new` already
  exists in some stores (e.g., a previously-deleted account's
  leftover etcd keys)? Refuse with a list, or coerce? Refusing is
  simpler and safer.
