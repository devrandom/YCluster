# Authentik groups

Authentik groups serve two distinct purposes in the cluster. It's easy to
confuse them because both live in the same directory and both sound
"admin".

## App-role groups (ycluster)

Bootstrap-created by the admin blueprint (`authentik-manager`); consumed by
the web app via the `X-Authentik-Groups` forward-auth header:

- `ycluster-admins` — full access to every admin page and mutation in the
  Flask admin-api. Does **not** grant access to authentik's own admin UI.
- `ycluster-managers` — view access to admin pages, scoped to authorized
  projects (see [users.md](users.md)).
- `comfy` — gates access to the ComfyUI surface (`comfy.<domain>`); any
  member (or any `ycluster-admins` member) can sign in. See
  [comfyui-h3.md](../usage/comfyui-h3.md).

These groups are referenced by name in nginx forward-auth policies and in
`app.py` (`_authentik_identity()`). Adding a user to them changes what the
**app** lets them do, nothing else.

## IdP-superuser group (authentik Admins)

`authentik Admins` is authentik's own built-in superuser group. Membership
controls who can administer the IdP itself: Directory, Applications,
Flows, Providers, Policies, and so on. It is independent of the
app-role groups above — a `ycluster-admins` member cannot manage authentik
unless they are also in `authentik Admins`.

Implementation note: authentik's `User.is_superuser` is a **cached property
derived from group membership** (any group with `is_superuser=True`), not a
writable DB column. Setting the field directly (e.g. via Django shell) has
no effect; the only way to grant it is to add the user to a superuser
group.

The `akadmin` bootstrap user is also a superuser. Its password lives in
etcd at `/cluster/config/authentik/bootstrap-password` (written by
`authentik-manager` on first boot). Prefer adding your own account to
`authentik Admins` over sharing `akadmin` interactively, so actions are
attributable.

## Adding a user to a group

Authentik admin UI (`https://auth.<domain>` → **Directory → Groups** → open
the group → **Users** → **Add existing user**), or from the storage leader:

```bash
docker exec -e DJANGO_SETTINGS_MODULE=authentik.root.settings \
    authentik-server python3 -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'authentik.root.settings')
django.setup()
from authentik.core.models import Group, User
u = User.objects.get(email='<user-email>')
g = Group.objects.get(name='<group-name>')
g.users.add(u)
print('ok', u.username)
" 2>/dev/null
```

Changes take effect on the user's next login (sessions don't re-evaluate
group membership mid-flight).
