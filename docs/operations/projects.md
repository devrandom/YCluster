# Projects: IdP projects vs user projects

There are two distinct "project" concepts in the cluster. They serve
different purposes and live in different systems. This page exists because
conflating them caused real confusion — if you are touching user scoping,
quotas, or the user projects admin page, read this first.

## IdP projects (Authentik)

- **What:** a free-text `project_name` attribute on each Authentik user,
  plus a `manager_projects` list on manager accounts. See
  [users.md](users.md) and
  [manager-project-scoped-users.md](../design/manager-project-scoped-users.md).
- **Where it lives:** Authentik's user attributes JSON.
- **Purpose:** organizational grouping and access control. "This user
  belongs to ACME", "this manager may operate on ACME and Billing users".
- **Consumers:** `/admin/users` roster scoping (managers see users whose
  `project_name` matches their `manager_projects`), invitations.
- **Cardinality:** one IdP project per user (the attribute is a single
  string); managers may be authorized for several.
- **Character:** free text, no validation, no integrity guarantees. It is
  convention, not structure.

## User projects (`admin` PostgreSQL)

- **What:** first-class project objects with membership and quotas. See the
  User projects section of [users.md](users.md).
- **Where it lives:** the `admin` PostgreSQL database
  (`projects`, `project_members`, `project_quotas` tables), managed via
  `/admin/projects` with a full audit trail.
- **Purpose:** resource accounting and, eventually, enforcement. Every
  user gets a personal default user project; additional shared projects
  can be created.
- **Membership:** relational — `project_members` rows keyed by Authentik
  UID, with one default project per user (partial unique index). Emails are
  captured at assignment time for readability only; the UID is authoritative.
- **Cardinality:** many-to-many. A user can be a member of several user
  projects; one is their default.
- **Nesting:** each user project may carry an `idp_project` value linking it
  to the IdP project it is nested under. This controls manager visibility:
  managers see user projects whose `idp_project` is in their
  `manager_projects` list. An empty `idp_project` means the user project is
  admin-only.

## The nesting relationship

User projects are nested *inside* the IdP project concept: an IdP project
like `acme` might map to one or more user projects (e.g. one per team or
per GPU pool). They are not synchronized automatically — the mapping is an
operational decision made by admins when creating user projects and
assigning members.

## Manager visibility rules (current)

- `/admin/users`: managers see users whose Authentik `project_name` is in
  their `manager_projects` list (IdP-project scoping).
- `/admin/projects`: admins get full control; managers get read-only
  visibility of user projects whose `idp_project` matches their
  `manager_projects` list. Mutations (create, edit, members, quotas,
  import) remain admin-only.

When changing anything that reads or writes projects, state explicitly
which concept you mean — "IdP project" or "user project" — in code,
commit messages, and conversation.
