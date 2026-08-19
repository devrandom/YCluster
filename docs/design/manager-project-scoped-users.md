# Manager project-scoped actions on `/admin/users`

Status: approved (implemented)

## Goal / scope

Turn managers from pure read-only viewers into project-scoped operators on
the Users page (`/admin/users`):

1. Edit the **Notes** field.
2. **Invite** and issue password **(re)set links**, but only for projects
   they're authorized for (default: none).
3. See **outstanding invitations** only for projects they're authorized for.

Everything else stays admin-only: role changes (make/revoke admin or
manager), enable/disable, name/project editing, and revoking invitations.

## Data model

**Manager's authorized projects** — stored like the existing per-user
metadata, in authentik's user `attributes` JSON, as a new key
`manager_projects` (list of project-name strings). Default/empty =
authorized to *none*. Admins are implicitly authorized to everything
(bypass checks); the key is only meaningful on manager accounts.

- Managed via a new CLI group (below). Optional later: an admin-only
  field in the UI.
- Matches the existing convention (`project_name` / `admin_notes` already
  live in `attributes` and are read-modify-write merged in `update_user()`).

**Project on invitations** — invitations get an optional `project_name` in
their `fixed_data`. Admins may invite without a project (untagged = "no
project", visible to admins only); managers must pick one of their
authorized projects and the invite is tagged with it.

**Account project** — `project_name` already exists on the user record
(free-text attribute). Authorization is an exact-match between the
manager's `manager_projects` list and `user.project_name` (or
`invite.project_name`).

## Authorization matrix

| Capability | Admin | Manager |
|---|---|---|
| View full roster | yes | **only users whose `project_name` is in `manager_projects`** (none by default) |
| Edit Notes field | any user | **only users whose `project_name` is in their `manager_projects`** |
| Invite | any (project optional) | **only into a project in `manager_projects`** (project required) |
| Password (re)set link | any user | **only users whose `project_name` is in `manager_projects`** |
| See outstanding invites | all | **only invites tagged with a project in `manager_projects`** |
| Revoke invite / role changes / enable / disable / edit name+project | yes | 403 (unchanged) |

A manager with no `manager_projects` gets no invite form, no
reset/edit affordances, and an empty invitations table — the "default to
none" behavior.

## Backend changes

**`/admin/users/data`** — response gains capability flags so the page can
render the right controls:

- keep `is_admin`
- add `manager_projects` (list; empty for admins is treated as
  unrestricted)
- compute editor booleans server-side

**`invitations_data()`** — return each invite's `project_name`; accept an
optional authorized-project filter. The route filters for managers (admins
get everything). Existing untagged/legacy invites have no project →
invisible to managers (correct default).

**`users_action()`** — split gating from `is_admin` into per-action rules
and enforce project checks server-side (the UI is only a veneer; the
backend is the gate):

- `edit`: admins as today (name/project/notes). Managers get a restricted
  path that **only permits `admin_notes`** and only when the target user's
  `project_name` ∈ `manager_projects`.
- `invite`: managers must supply a `project_name` ∈ `manager_projects`; the
  invite is created with that tag. Admins keep the existing
  (project-optional) path.
- `recovery`: managers only when target user's `project_name` ∈
  `manager_projects`. Admins unchanged.
- `uninvite`, `admin_*`, `manager_*`, `activate`, `deactivate`: still
  `is_admin`-only.

**Enrollment blueprint** (`authentik-blueprint-enrollment.yaml`) — so an
invited user actually lands in their project, extend the `user-write` stage
to persist `project_name` from the invitation `fixed_data` into
`attributes.project_name`. (The one integration point with real IdP
friction — see Decisions.)

## UI changes (`admin-users.html`)

- Replace the single `isAdmin` gate with role + capability flags from
  `/admin/users/data`.
- **Invite form**: admins as today (project optional). Managers get a
  project `<select>` populated from `manager_projects`; required; the whole
  form is hidden when the list is empty.
- **Row action menu**: admins get the full menu. Managers get a reduced
  menu with only "Edit notes…" and "Reset link", each only presented when
  the row's user is in an authorized project (server still enforces).
- **Edit modal**: for managers, render only the *Notes* textarea
  (email/name/project read-only or hidden); admins keep the current full
  form.
- **Outstanding invitations**: server already filters for managers; hide
  the revoke button for non-admins (already done).

## CLI changes (`ycluster/user.py` + `authentik_manager.py`)

New subcommand to manage a manager's authorized projects:

```
ycluster user manager-projects <email> list
ycluster user manager-projects <email> add acme-demo
ycluster user manager-projects <email> remove acme-demo
ycluster user manager-projects <email> set acme-demo billing-ops   # replace list
```

Backed by a small `authentik_manager.manager_projects` /
`set_manager_projects` that read-modify-write the `manager_projects`
attribute (same merge pattern as `update_user`). Admin grant/revoke pages
and `ycluster user manager` keep working; revoking the manager role leaves
the attribute but it's inert while not a manager.

## Docs & tests

- `docs/operations/users.md`: update the capability table and add a
  "Manager project authorization" section.
- Unit tests in `test_app.py` (authentik stubbed): managers can edit notes /
  invite / recovery / see invites *within* an authorized project; 403 for
  unauthorized project, untagged invites hidden, name/project edit still
  blocked, revoke still admin-only; admins fully unchanged.

## Decisions

Confirmed with the requester:

1. **Notes editing is project-scoped** — a manager may edit Notes only for
   users in projects they're authorized for (never name/project fields).
2. **Manager invite UX** — managers must pick an authorized project (no
   untagged invites); revoke stays admin-only.
3. **Enrollment attribute mapping** — the blueprint `user-write` stage is
   extended so the invitation `project_name` is written onto the enrolled
   account; this is what makes the chosen project stick to the account.
4. **Authorized-project management is CLI-only for now** — `ycluster user
   manager-projects`; an admin UI field in `/admin/users` is a possible
   follow-up.

