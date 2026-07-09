# Cluster users (IdP accounts)

Accounts live in the cluster IdP (authentik) — there is no separate user
database. Authentik is provisioned by
[`install-authentik.yml`](../../config/ansible/app/install-authentik.yml);
its admin blueprint (`ycluster-admin.yaml`, written by `authentik-manager`)
creates two groups the web app cares about:

- `ycluster-admins` — full access to every admin page and mutation.
- `ycluster-managers` — view-only access to every admin page (no mutations).

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
| `/admin/vm-schedule/set`, `stop-now` | yes | **403** | owner-only |
| `/admin/users` (roster + invitations) | yes | read-only | 403 |
| `/admin/users/action` (invite, role changes, edit, disable) | yes | **403** | 403 |
| `/api/inventory/asset/<host>` PUT/POST | yes | **403** | 403 |
| `/admin/settings` (own SSH keys for VM access) | self | self | self |

A manager is "an admin minus the mutations" — admins implicitly pass every
manager check (single boolean inheritance, no double bookkeeping).

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
```

Hand the URL over out-of-band — there's no SMTP in the cluster.

For an account that already exists but needs a password reset:

```
ycluster user recovery alice@x
```

The CLI never sees the password; the user sets it via the link.

## Invitations

Outstanding invitations are listed on `/admin/users` (admins can revoke;
managers see the list, no revoke). CLI:

```
ycluster user invitations       # list
ycluster user uninvite alice@x  # revoke
```

## Disable / re-enable

Disabling blocks login without removing the account or its data (API keys,
VM ownership, etc.).

```
ycluster user disable alice@x
ycluster user enable  alice@x
```

## Internal space (operators)

Cluster-internal callers (`admin.xc`, direct `:12723`) carry no
forward-auth headers and are treated as admins with no email — see
`_authentik_identity()` in `app.py`. There's no separate "operator role";
operators either log in via the IdP (and get a real role) or hit the
internal vhost directly.
