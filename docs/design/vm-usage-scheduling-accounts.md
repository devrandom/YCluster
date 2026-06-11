# VM usage tracking, scheduling, and account management

## Status

**Draft — direction decided, nothing implemented.** Decided so far:

- Continue with owned VMs (see `docs/operations/vm-hosting.md`), not Slurm.
  Slurm answers "run this batch job somewhere"; our users want "my machine,
  up when I need it". Revisit if batch workloads emerge — Slurm could then
  run *inside* scheduled VMs.
- Track usage two ways in parallel — a lifecycle **event log** (authoritative
  for billing) and periodic **samples** of actual incus state (cross-check) —
  so admin debugging sessions don't bill users, and discrepancies are
  detectable rather than silently wrong.
- **IdP first**: stand up a cluster identity provider before the scheduling
  web page, because that page needs browser login and no session auth exists
  anywhere today. Open-WebUI stops being the system of record for accounts
  and becomes an OIDC client.
- **Internal accounts are first-class** — no hard dependency on an
  external service to log in. GitHub / GitLab sign-in is an optional
  convenience, linked to an existing internal account. No org gating.
- **Enrollment is invitation-gated**, not open: admins issue single-use
  invite links (`ycluster user invite`); strangers cannot create
  accounts in the IdP even unauthorized ones.

## Motivation

GPU VMs are allocated to users but nothing records how long they run or how
many GPU-hours they consume. We want: a web page where users schedule their
VMs to be up, per-user GPU-hour accounting, and (later) quotas. That chain
is blocked on two gaps:

1. **No identity layer.** User identity lives in Open-WebUI's `user` table
   and is only reachable via its `api_key` table (`local-ai-proxy-auth`).
   There is no browser session auth: `admin.xc` is unauthenticated and the
   nginx `auth_request` pattern is bearer-token-only.
2. **No usage records.** `/cluster/vms/<name>` has `owner` and `gpus`, but
   `vm start`/`vm stop` write nothing anywhere — and a manual `incus start`
   on the host bypasses the CLI entirely.

## Phase 1 — Identity provider

A small self-hosted OIDC IdP becomes the system of record for accounts.
Everything else (Open-WebUI, the admin/scheduling pages, future API-key
issuance) consumes it.

Requirements:

- OIDC provider downstream (Open-WebUI, admin pages, future services).
- **Internal accounts are first-class** — the cluster must never
  hard-depend on an external service to log in.
- **GitHub / GitLab sign-in is optional convenience**, linked to an
  existing internal account — a login *method*, not an account *source*.
- **No org gating.** Authorization is the IdP's own account registry,
  admin-provisioned. An external login that doesn't link to an existing
  account gets nothing.
- Config-as-code / Ansible-managed as far as practical; identity keyed
  by email (matches existing attribution keys).

**Recommendation: Authentik.**

- The account model fits exactly: public enrollment is closed; onboarding
  is by **single-use invitation link** (authentik invitations) — the user
  self-enrolls through the link with GitHub/GitLab or an internal
  password, arriving pre-bound to the invited email. GitHub/GitLab are
  configured as *sources* whose open enrollment is denied — outside an
  invitation, an external login succeeds only by linking (email match) to
  an existing account. Every user can always fall back to internal login,
  so no external dependency.
- Native nginx `auth_request` support via its embedded outpost
  (forward-auth mode) — no oauth2-proxy sidecar; backends receive
  `X-authentik-email` / `X-authentik-groups` headers.
- "Blueprints" give declarative YAML for providers, sources and flows, so
  the setup is largely Ansible-managed; the user accounts themselves live
  in Postgres (covered by existing DB operations/backups).
- Cost: the heaviest of the acceptable candidates — Django server +
  worker + Redis + Postgres, run as containers on the storage leader the
  same way Open-WebUI is.

Alternatives considered and why not:

- **Authelia** — config-as-code and native `auth_request`, but **no
  upstream IdP federation** (long-open feature request): no GitHub/GitLab
  login at all.
- **Dex** — built for federation, but *only* a federator: without org
  gating it has no authorization story (any GitHub account authenticates;
  allow-lists would have to be duplicated in every consumer), and its
  local accounts (`staticPasswords`) lack password change and MFA — not
  first-class.
- **Keycloak** — does identity brokering + account linking fine, but the
  most machinery of all, and realm config drifts in its UI.
- **Zitadel** — plausible middle ground (Go, Postgres, external IdPs),
  but configuration is API/UI-driven rather than declarative.
- **Dex + Authelia (composed)** — the serious runner-up. Dex as the
  federation hub with GitHub, GitLab *and Authelia* (an OIDC provider,
  just not a consumer) as upstream connectors; Authelia supplies
  first-class internal accounts with MFA. Maximally config-as-code:
  three small Go binaries, all static YAML. Rejected because Dex has no
  account registry, which leaves three gaps: no account linking (same
  person via GitHub vs internal is an email-match convention, not a
  mechanism); no central authorization point (any GitHub user
  authenticates, so allowlists must live in every consumer —
  oauth2-proxy email file, Open-WebUI pending-role gate, and each future
  app); and Authelia's forward-auth is unusable in this topology
  (browser sessions belong to Dex), so oauth2-proxy comes back as a
  third component. Revisit if Authentik's weight becomes a problem and
  per-consumer allowlists are acceptable.

Swapping later stays cheap as long as consumers only speak OIDC.

Linking note: external-login linking is by email match, so a user's
GitHub/GitLab primary verified email must equal their account email.
If it doesn't, the external login simply fails to link — they use
internal login, or the admin links the source to their account manually.

### Deployment shape

- Authentik (server + worker + Redis) runs as containers on the storage
  leader under the existing leader-election pattern (like Open-WebUI),
  state in a dedicated Postgres database; providers/sources/flows
  declared as blueprints rendered by Ansible.
- nginx vhost `auth.xc` internally; exposed externally through the
  existing rathole/nginx front — needed for users logging in from outside
  and for GitHub/GitLab OAuth callbacks.
- GitHub / GitLab OAuth apps are **optional**: when configured (client
  IDs/secrets in the Ansible vault) they appear as alternate login
  buttons; when absent, internal login is unaffected.
- Account management goes through a new CLI subcommand wrapping the
  authentik API, consistent with the existing `vm ssh add` flow:

  ```bash
  ycluster user add alice@example.com      # create account (pre-link email)
  ycluster user invite alice@example.com   # print single-use invite URL
  ycluster user list
  ```

- Accounts are keyed by **email** — the same key already used by
  `/cluster/vms` `owner`, `/cluster/users`, and `model_usage.user_id`,
  so usage attribution needs no mapping table. Longer term, `ycluster
  user` is the natural home for unifying the IdP registry with
  `/cluster/users` (VM SSH keys).

### Open-WebUI becomes a client

- `ENABLE_OAUTH_SIGNUP=true`, `OAUTH_CLIENT_ID/SECRET`,
  `OPENID_PROVIDER_URL=<authentik discovery URL>`, and critically
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` so existing accounts (and their API
  keys) survive the cutover.
- Password login stays enabled during a transition window, then
  `ENABLE_LOGIN_FORM=false`.
- **Migrating existing accounts**: seed authentik from OWUI's `user`
  table (one-shot script over the authentik API / `ycluster user add`;
  map `role='admin'` to an authentik admin group), then send each user
  an invite to set credentials or link GitHub/GitLab. Password hashes
  are deliberately *not* migrated (OWUI's bcrypt rows could be written
  into authentik's Django schema, but that's an unsupported hack — the
  invite flow replaces it). Nothing else moves: merge-by-email lands
  each OIDC login in the user's existing OWUI account, so chats,
  settings and `api_key` rows survive untouched.
- `local-ai-proxy-auth` is **unchanged**: inference API keys continue to
  live in Open-WebUI's `api_key` table. Moving key issuance into our own
  service is a later, independent step (see Open questions).

### Admin pages get sessions

A new nginx location class on `admin.xc`: `auth_request` to authentik's
embedded outpost (forward-auth), which redirects browsers to the login
flow on first visit. The Flask admin app trusts the `X-authentik-email` /
`X-authentik-groups` headers (set only by nginx; the app must continue
binding to localhost / VIP-internal). Existing
unauthenticated read-only pages (`/status`, `/inventory`) can stay open or
move behind auth — decide at implementation.

## Phase 2 — VM usage tracking

Two record streams into the existing `usage_stats` Postgres database
(same home as `model_usage`), reconciled against each other.

### Lifecycle event log (authoritative for billing)

`vm_manager.py` emits an event on every lifecycle mutation —
launch / start / stop / destroy / GPU-count change:

```
vm_events(ts, vm, host, event, owner, gpus, initiator, billable)
```

- `initiator`: `web:<email>` (scheduling page), `scheduler` (reconciler
  acting on a user's schedule), or `cli` (admin on the host).
- `billable`: **CLI operations default to non-billable** — an admin
  bringing a VM up for debugging must not count against the owner's quota.
  A `--bill` flag opts in when the admin is acting on the owner's behalf.
  Web/scheduler-initiated operations are billable.
- Billable GPU-hours = Σ over billable start→stop intervals of
  `gpus × duration`.

Transport: VM hosts already read/write etcd, but Postgres access is
leader-only by convention. Events are therefore appended to an etcd queue
(`/cluster/vms/events/<seq>`) and drained into Postgres by the leader-side
collector (next section), which deletes them after insert. One etcd put
per lifecycle op — negligible volume.

### Sampling (cross-check)

A leader-elected collector (sibling of `collect-model-stats`, same
timer-unit pattern, coarser interval — every 1–5 min) polls each VM host
for actually-RUNNING VMs and their GPU counts, and accrues:

```
vm_samples(ts, vm, host, owner, gpus, state)
```

Poll path: a new endpoint on the admin-api that already runs on every node
(e.g. `GET /api/vms` → local `incus list --format json`, VM-host nodes
only). Avoids opening Postgres or ssh fan-out from the leader.

### Reconciliation

Per VM per day, compare event-derived running-hours against sample-derived
running-hours. Discrepancies mean: manual `incus start/stop` that bypassed
the CLI, a host reboot with autostart, a missed event, or clock skew. The
dashboard surfaces them; resolution is human at first (decide whether the
time was billable). Samples are the safety net — they make untracked
runtime *visible* without automatically billing it.

### Dashboard

`/admin/vm-usage` on the admin app, modeled on `/admin/model-usage`:
per-user and per-VM GPU-hours over a period, billable vs observed
(sampled) hours side by side, discrepancy flag.

## Phase 3 — Scheduling page + reconciler

Desired-state reconciliation, the idiom the cluster already speaks:

- **Schedule model**: per VM, weekly recurring windows (e.g. Mon–Fri
  08:00–20:00) plus one-off overrides ("up for the next 4 h", "down this
  week"). Stored in Postgres; edited via the web page.
- **Reconciler**: leader-elected timer compares desired vs actual state
  and issues start/stop through the same admin-api host endpoint
  (POST, host-local incus calls → `vm_manager`, so events are emitted with
  `initiator=scheduler`).
- **Stops must be graceful** — never `incus stop --force` on a GPU VM
  (FLR wedge, see vm-hosting.md "Critical rule"). A scheduled stop sends a
  wall warning into the guest and waits a grace period; clean shutdown
  only. If a guest refuses to shut down, alert — don't force.
- **Page**: users (via IdP session) see and edit schedules for VMs
  where `owner == Remote-Email`; `admin` group sees all.

### GPU reservation vs runtime (important wrinkle)

A *stopped* VM still holds its passthrough GPUs — the incus device entries
keep the PCI addresses allocated, so scheduling a VM down does **not**
return its GPUs to the pool. For scheduling to actually share hardware,
the reconciler should (optionally, per VM) detach GPU devices on scheduled
stop and re-attach on start from the free pool — exactly the manual
procedure used to resize vm1, and the planned `vm set-gpus` subcommand
(TODO.md). This also opens the door to billing *reservation* hours vs
*runtime* hours differently. Defer to a later iteration, but design the
schedule schema with a `release_gpus_on_stop` flag from the start.

## Phase 4 — Quotas

Once tracking has produced a few weeks of real data:

- Per-user GPU-hour budget per period, stored alongside the schedule data.
- Enforcement point is the **reconciler/web layer only**: it refuses to
  start (or schedule) a VM whose owner is over budget. The CLI is never
  blocked — admins retain full manual control, and CLI ops are
  non-billable anyway.
- Soft-limit warnings on the page before hard refusal.

## Implementation order

Estimates are focused working days, including cluster testing, Ansible
idempotency, and doc updates.

1. Authentik: playbook (`app/install-authentik.yml`), Postgres DB,
   blueprints (incl. invitation-gated enrollment flow), nginx vhost +
   external exposure; optional GitHub/GitLab OAuth apps (secrets in
   vault); `ycluster user add/invite/list` wrapping the authentik API.
   — **3–5 d**. The widest-variance step: authentik's flow/blueprint
   model has a real learning curve, and the invitation + source-linking
   flows need iteration against a live GitHub OAuth app.
2. Open-WebUI OIDC cutover (merge-by-email, then disable login form).
   — **0.5–1 d** of work, plus a multi-day soak with both login methods
   enabled before turning the password form off. Verify api_keys survive
   for every existing account before the soak ends.
3. Forward-auth on `admin.xc` locations. — **0.5–1 d** (nginx location
   class + header trust in the Flask app).
4. Event log: `vm_manager` events → etcd queue; admin-api `GET /api/vms`;
   leader collector (drain events + sample) + `usage_stats` tables.
   — **2–3 d**; the collector and schema follow the `collect-model-stats`
   pattern closely, the new surface is the etcd queue drain semantics.
5. `/admin/vm-usage` dashboard with discrepancy view. — **1–2 d**
   (near-clone of `/admin/model-usage` + the reconciliation query).
6. Scheduling page + reconciler (graceful stops). — **3–5 d**: schedule
   schema + editing UI ~2 d; reconciler ~1 d; the rest is the graceful
   stop path (wall warning, grace period, refuse-to-force, alerting on a
   guest that won't shut down) — the part that must not be rushed, given
   the FLR-wedge failure mode.
7. Quotas. — **1–2 d** once 4–6 exist (budget table, reconciler check,
   page warnings).

Total ≈ **11–19 days**. Each step is independently shippable; 4–5 don't
depend on 1–3 (tracking needs no login), so the two tracks can proceed in
parallel. The riskiest items for schedule slip are authentik flow
iteration (1) and graceful-stop edge cases (6); everything else follows
established in-repo patterns.

## Open questions

- **API-key issuance**: should keys eventually move out of Open-WebUI's
  `api_key` table into a small service of ours (issued against IdP
  identities), with `local-ai-proxy-auth` checking both during migration?
  Decide after the IdP has been in service for a while.
- **Unattributed runtime**: when samples show running time with no billable
  interval (manual incus ops, autostart after host reboot), do we ever
  auto-bill the owner, or always leave it to human review? Start with
  review-only.
- **Existing read-only admin pages**: keep `/status` / `/inventory` open
  inside the cluster, or move everything behind auth?
- **Reservation billing**: bill held-but-stopped GPUs at a reduced rate to
  discourage hoarding, once `release_gpus_on_stop` exists?
