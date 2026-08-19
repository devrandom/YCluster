# Cluster users (IdP accounts)

Accounts live in the cluster IdP (authentik) — there is no separate user
database. Authentik is provisioned by
[`install-authentik.yml`](../../config/ansible/app/install-authentik.yml);
its admin blueprint (`ycluster-admin.yaml`, written by `authentik-manager`)
creates two groups the web app cares about:

- `ycluster-admins` — full access to every admin page and mutation.
- `ycluster-managers` — view access to every admin page (same visibility
  as an admin), and the same owner-edit rights as a plain user on VMs
  they happen to own. Cannot edit other users' VMs, set any VM
  unmanaged, change inventory, or manage accounts/SSH-keys for users
  other than themselves.

Both groups are bootstrap-created; re-running the install playbook is a
no-op when they already exist.

## Roles and what they can do

The Flask `admin-api` reads three headers set by nginx's forward-auth:

- `X-Authentik-UID` — primary identity signal (always present for any
  authentik-authenticated user, including OAuth-sourced accounts without
  a verified email).
- `X-Authentik-Email` — optional metadata. Missing for OAuth-sourced
  users whose upstream provider doesn't share a verified email.
- `X-Authentik-Groups` — pipe-joined group memberships.

Internal callers (admin.xc, direct `:12723`) carry no headers and are
treated as admins (operator space) — see `_authentik_identity()` in
`app.py` for the two-tier loopback guard.

### OAuth-sourced users without verified email

A user authenticated via GitHub/GitLab whose upstream profile doesn't
share an email will arrive at admin-api with `X-Authentik-UID` set but
`X-Authentik-Email` empty. This is fine for role checks (the UID drives
the group authorization), but those users:

- Cannot manage VMs in `/admin/vm-schedule` (ownership is keyed by email,
  and the page filter `rec.get('owner') == email` will exclude them).
- Cannot manage SSH keys in `/admin/settings` (per-user records are
  keyed by email at `/cluster/users/<email>`).

The audit log's `updated_by` field falls back to UID when email is
missing, so changes are still attributable. To enable VM/SSH-key
management for these users, set their email in Authentik (Settings →
Users → edit).

| Capability | Admin | Manager | Plain user |
|---|---|---|---|
| `/admin/utilization`, `/admin/inventory`, `/admin/resources`, `/admin/model-usage`, `/admin/vm-usage` | full | full | read-only (some) / 403 |
| `/admin/vm-schedule` view all VMs + owner emails | yes | yes | own VMs only, owners redacted |
| `/admin/vm-schedule/set` (on|off|schedule), `stop-now` | yes | **owner-only** | owner-only |
| `/admin/vm-schedule/set mode=unmanaged` (admin escape hatch) | yes | **403** | 403 |
| `/admin/users` (roster + invitations) | yes | roster + invites for **authorized projects only** | 403 |
| `/admin/users/action` (invite / reset / edit notes) | yes | **authorized-projects only** | 403 |
| `/admin/users/action` (role changes, enable/disable, revoke invite) | yes | **403** | 403 |
| `/api/inventory/asset/<host>` PUT/POST | yes | **403** | 403 |
| `/admin/settings` (own SSH keys for VM access) | self | self | self |

A manager is "an admin for visibility" — sees every admin page the same way
an admin does, except the `/admin/users` roster (which is project-scoped).
On the mutation side they inherit the same owner-only
constraints as a plain user for VMs, and on `/admin/users` they get
**project-scoped** user management (edit Notes, invite, and issue
reset links) only for projects they're authorized for — see [Manager
project authorization](#manager-project-authorization) below. Admins
implicitly pass every manager check (single boolean inheritance, no double
bookkeeping).

## Manager project authorization

Managers can operate on `/admin/users` only within projects they've been
granted. Given a manager `bob@x`, authorized projects default to **none**:

```
ycluster user manager-projects bob@x list                # (none) by default
ycluster user manager-projects bob@x add acme-demo       # authorize one
ycluster user manager-projects bob@x add acme-demo billing-ops
ycluster user manager-projects bob@x remove acme-demo    # de-authorize one
ycluster user manager-projects bob@x set acme-demo       # replace the whole list
```

With projects authorized, on `/admin/users` a manager can:

- **See the roster**: only users whose `project_name` is one of their
  authorized projects (with none authorized, the roster is empty).
- **Edit Notes** on any user whose `project_name` is one of their
  authorized projects (never name/project fields).
- **Invite** only *into* one of their authorized projects (the invite and
  the enrolled account are tagged with that project; a manager cannot make
  untagged invites).
- **Issue a password (re)set link** for a user in an authorized project.
- See **outstanding invitations** only for their authorized projects.

Everything else on the users page stays admin-only: making/revoking
admin or manager, enable/disable, editing name/project, and revoking
invitations. The backend enforces these checks regardless of what the UI
shows.

## Granting / revoking roles

CLI:

```
ycluster user admin    alice@x            # grant admin
ycluster user admin    alice@x --remove   # revoke admin
ycluster user manager  bob@x              # grant manager
ycluster user manager  bob@x --remove     # revoke manager
```

Web UI (`/admin/users`): per-user menu (`…`) → "Make admin" / "Revoke
admin" / "Make manager" / "Revoke manager". The whole menu is hidden from
non-admins.

List accounts and their roles:

```
ycluster user list
```

## Creating an account

Two paths; pick whichever fits the onboarding story.

**External login** (GitHub/GitLab): the user logs in via the IdP, and
authentik creates the account. Set the
`authentik_github_client_id/_secret` (or `_gitlab_`) vault vars and run the
OAuth-sources blueprint (`install-authentik.yml`). The user can then be
promoted to admin/manager.

**Internal password**:

```
ycluster user invite alice@x                       # one-use enrollment link
ycluster user invite alice@x --name "Alice" --days 14
ycluster user invite alice@x --project acme-demo   # tag invite + enrolled account with a project
```

Hand the URL over out-of-band — there's no SMTP in the cluster.

For an account that already exists but needs a password reset:

```
ycluster user recovery alice@x
```

The CLI never sees the password; the user sets it via the link.

## Invitations

Outstanding invitations are listed on `/admin/users` (admins see and can
revoke all; managers see only their authorized projects' invites, revoke is
admin-only). CLI:

```
ycluster user invitations                          # list
ycluster user uninvite alice@x                     # revoke
ycluster user invite-project alice@x acme-demo     # tag a pending invite with a project
```

An invitation tagged with a project via `invite-project` (or created with
`invite --project`) writes that project onto the account when it enrolls,
and — for managers — makes it visible in their outstanding-invites view
only if the project is one they're authorized for.

## Disable / re-enable

Disabling blocks login without removing the account or its data (API keys,
VM ownership, etc.).

```
ycluster user disable alice@x
ycluster user enable  alice@x
```

## Per-user metadata

Three fields are surfaced on the accounts table (`/admin/users`):

- **Created** — read-only, from authentik's `date_joined`.
- **Project** — free-text, admin-editable. Useful for billing/cost
  allocation.
- **Notes** — free-text (multi-line), admin-editable. Internal admin
  notes about the account.

Both writable fields live in authentik's per-user `attributes` JSON
(under `project_name` and `admin_notes`). Edit via the Edit… action in
the row menu, or from the CLI:

```
ycluster user edit alice@x --project "acme-demo" --notes "VIP customer"
ycluster user edit alice@x --name "Alice Doe"     # name still editable too
```

Visible to anyone who can view `/admin/users` (admins and managers —
same scope as the rest of the page).

## Internal space (operators)

Cluster-internal callers (`admin.xc`, direct `:12723`) carry no
forward-auth headers and are treated as admins with no email — see
`_authentik_identity()` in `app.py`. There's no separate "operator role";
operators either log in via the IdP (and get a real role) or hit the
internal vhost directly.
