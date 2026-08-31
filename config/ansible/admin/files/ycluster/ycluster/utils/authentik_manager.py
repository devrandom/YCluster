"""
Account management against the cluster IdP (authentik).

Authenticates with the bootstrap API token that authentik-manager
generates once into etcd. The base URL defaults to the cluster-internal
nginx vhost; override with YC_AUTHENTIK_URL (e.g. http://127.0.0.1:9300
when running on the storage leader without DNS).

Operations return data (the admin-api /admin/users page consumes them
as JSON); the `ycluster user` CLI wrappers do the printing.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..common.etcd_utils import get_etcd_client

ENROLLMENT_FLOW_SLUG = 'enrollment-invitation'
TOKEN_ETCD_KEY = '/cluster/config/authentik/bootstrap-token'
ADMIN_GROUP = 'ycluster-admins'
MANAGER_GROUP = 'ycluster-managers'

def _base_url():
    return os.environ.get('YC_AUTHENTIK_URL', 'http://auth.xc').rstrip('/')

def _link_base_url():
    """Base for URLs handed to users (invites, recovery links): always the
    external domain — users arrive from outside, where auth.xc does not
    resolve. YC_AUTHENTIK_LINK_URL overrides (dev/testing)."""
    override = os.environ.get('YC_AUTHENTIK_LINK_URL')
    if override:
        return override.rstrip('/')
    client = get_etcd_client()
    value, _ = client.get('/cluster/https/domain')
    if not value:
        raise RuntimeError(
            "no external domain configured (/cluster/https/domain) — user-facing "
            "links require one. Set it with 'ycluster https set-domain', or set "
            "YC_AUTHENTIK_LINK_URL to override.")
    return f"https://auth.{value.decode().strip()}"

def _api_token():
    client = get_etcd_client()
    value, _ = client.get(TOKEN_ETCD_KEY)
    if not value:
        raise RuntimeError(
            f"No authentik API token at {TOKEN_ETCD_KEY} — has authentik started once?")
    return value.decode().strip()

class AuthentikAPI:
    def __init__(self):
        self.base = _base_url() + '/api/v3'
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {_api_token()}'

    def _request(self, method, path, **kwargs) -> Any:
        resp = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"authentik API {method} {path}: {resp.status_code} {resp.text}")
        # 204 No Content (e.g. core/groups/<pk>/add_user|remove_user) has an
        # empty body; resp.json() would raise. Return None so callers that
        # don't need the response can ignore it. GET callers always get
        # content; the declared Any return type keeps existing
        # `result['key']` indexing valid.
        if not resp.content:
            return None
        return resp.json()

    def get(self, path, **kwargs):
        return self._request('GET', path, **kwargs)

    def post(self, path, json):
        return self._request('POST', path, json=json)

    def patch(self, path, json):
        return self._request('PATCH', path, json=json)

    def flow_pk(self, slug):
        flows = self.get('/flows/instances/', params={'slug': slug})['results']
        if not flows:
            raise RuntimeError(f"flow '{slug}' not found — is the enrollment blueprint applied?")
        return flows[0]['pk']

def _get_user(api, email):
    """Find an account by the user-facing identifier. Most accounts have
    username == email, but authentik's default admin (akadmin) and any
    externally-sourced user have a separate username — fall back to the
    email column so those are reachable too."""
    users = api.get('/core/users/', params={'username': email})['results']
    if not users:
        users = api.get('/core/users/', params={'email': email})['results']
    if not users:
        raise RuntimeError(f"no account for {email}")
    return users[0]

def add_user(email, name=None, active=True):
    """Create an internal account keyed by email (no credentials — for
    external-login linking; use invite for internal-password onboarding)."""
    api = AuthentikAPI()
    user = api.post('/core/users/', json={
        'username': email,
        'email': email,
        'name': name or email,
        'type': 'internal',
        'is_active': active,
    })
    return {'email': email, 'pk': user['pk']}

def import_owui_users(dry_run=False):
    """Migrate Open-WebUI accounts into authentik by copying their bcrypt
    password hashes (Django BCryptPasswordHasher format, 'bcrypt$' + hash).
    Creates missing accounts (username = email) and writes the hash only
    where the account has no usable password (Django marks those with a
    '!' prefix) — never overwrites a password someone has set. The API
    cannot set a raw hash, so this writes authentik's DB directly; both
    databases are local, so it runs on the storage leader. Verification
    needs the bcrypt hasher that install-authentik.yml ships in authentik's
    user_settings.py."""
    import psycopg2

    api = AuthentikAPI()
    existing = {u['username']
                for u in api.get('/core/users/', params={'page_size': 500})['results']}

    owui = psycopg2.connect(host='localhost', dbname='openwebui',
                            user='openwebui', password='openwebui')
    with owui, owui.cursor() as cur:
        cur.execute('SELECT a.email, u.name, a.password, a.active'
                    ' FROM auth a JOIN "user" u ON u.id = a.id'
                    ' ORDER BY a.email')
        accounts = cur.fetchall()
    owui.close()

    results = []
    ak = psycopg2.connect(host='localhost', dbname='authentik',
                          user='authentik', password='authentik')
    with ak, ak.cursor() as cur:
        for email, name, pw_hash, active in accounts:
            if not (pw_hash or '').startswith('$2'):
                results.append({'email': email, 'action': 'skipped',
                                'detail': 'no bcrypt hash in Open-WebUI'})
                continue
            created = email not in existing
            if dry_run:
                results.append({'email': email, 'action': 'would-import',
                                'detail': 'new account' if created else 'existing account'})
                continue
            if created:
                add_user(email, name, active=active)
            cur.execute(
                "UPDATE authentik_core_user"
                " SET password = %s, password_change_date = now()"
                " WHERE username = %s"
                "   AND (password IS NULL OR password = '' OR password LIKE '!%%')",
                ('bcrypt$' + pw_hash, email))
            if cur.rowcount:
                results.append({'email': email, 'action': 'imported',
                                'detail': 'created account' if created else ''})
            else:
                results.append({'email': email, 'action': 'kept',
                                'detail': 'password already set in authentik'})
    ak.close()
    return results

def invite_user(email, name=None, days=7, project=None):
    """Issue a single-use enrollment invitation pre-bound to the email.
    If `project` is given, it is tagged onto the invitation (and written
    onto the enrolled account by the enrollment flow's user-write stage),
    so the outstanding-invite view and the account's project stay
    consistent. Returns the invitation URL."""
    api = AuthentikAPI()
    # Enrollment always creates the account, so an invitation for an
    # existing account would fail at the user_write stage. Refuse early.
    existing = api.get('/core/users/', params={'username': email})['results']
    if existing:
        raise RuntimeError(
            f"account {email} already exists — enrollment invitations are for "
            f"new accounts. Issue a password (re)set link instead "
            f"('ycluster user recovery {email}').")
    fixed_data = {
        'username': email,
        'email': email,
        'name': name or email,
    }
    if project:
        fixed_data['project_name'] = project
    invitation = api.post('/stages/invitation/invitations/', json={
        'name': 'invite-' + email.replace('@', '-at-').replace('.', '-'),
        'flow': api.flow_pk(ENROLLMENT_FLOW_SLUG),
        'single_use': True,
        'expires': (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        'fixed_data': fixed_data,
    })
    return f"{_link_base_url()}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={invitation['pk']}"

def users_data():
    """Accounts as a list of dicts (email, name, type, active, last_login,
    is_admin, is_manager, date_joined, project_name, admin_notes,
    manager_projects), service accounts excluded."""
    api = AuthentikAPI()
    users = api.get('/core/users/', params={'page_size': 500})['results']
    rows = []
    for u in users:
        if u['type'] not in ('internal', 'external'):
            continue
        group_names = [g.get('name') for g in (u.get('groups_obj') or [])]
        attrs = u.get('attributes') or {}
        rows.append({
            'email': u['email'] or u['username'],
            'name': u['name'],
            'type': u['type'],
            'active': bool(u['is_active']),
            'last_login': u['last_login'],
            'is_admin': ADMIN_GROUP in group_names,
            'is_manager': MANAGER_GROUP in group_names,
            'date_joined': u['date_joined'],
            'project_name': attrs.get('project_name') or '',
            'admin_notes': attrs.get('admin_notes') or '',
            'manager_projects': list(attrs.get('manager_projects') or []),
        })
    return sorted(rows, key=lambda r: r['email'])

def invitations_data(projects=None):
    """Outstanding invitations as a list of dicts (email, expires, url,
    project_name). If `projects` is given (iterable of authorized project
    names), only invitations tagged with one of those projects are returned
    — managers see only their own projects' invites; admins pass None to see
    everything (including untagged invites)."""
    api = AuthentikAPI()
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    link_base = _link_base_url()
    allowed = set(projects) if projects is not None else None
    rows = []
    for inv in invitations:
        fixed = inv.get('fixed_data') or {}
        project = fixed.get('project_name') or ''
        if allowed is not None and project not in allowed:
            continue
        rows.append({
            'email': fixed.get('email', inv['name']),
            'expires': inv['expires'],
            'url': f"{link_base}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={inv['pk']}",
            'project_name': project,
        })
    return rows

def _user_attributes(email):
    """The `attributes` JSON of an account (empty dict if none)."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    return user.get('attributes') or {}

def manager_projects(email):
    """Project names a manager account is authorized to operate in. Empty
    list = none (default). Admins ignore this — they are authorized
    everywhere — so the value is only meaningful on manager accounts."""
    return list(_user_attributes(email).get('manager_projects') or [])

def user_project(email):
    """The `project_name` attribute of an account ('' if unset)."""
    return _user_attributes(email).get('project_name') or ''

def set_manager_projects(email, projects):
    """Replace the `manager_projects` attribute for an account (deduped,
    order-preserving, empty strings dropped). Returns a confirmation."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    attrs = dict(user.get('attributes') or {})
    clean = list(dict.fromkeys(p for p in projects if p))
    attrs['manager_projects'] = clean
    api.patch(f"/core/users/{user['pk']}/", json={'attributes': attrs})
    return (f"manager projects for {email}: "
            f"{', '.join(clean) if clean else 'none'}")

def set_admin(email, remove=False):
    """Add (or remove) an account to the ycluster-admins group, which gates
    the forward-auth'd admin web pages. Returns the action performed."""
    return _set_group_membership(email, ADMIN_GROUP, remove=remove)

def set_manager(email, remove=False):
    """Add (or remove) an account to the ycluster-managers group, which grants
    view-only access to the admin web pages. Returns the action performed."""
    return _set_group_membership(email, MANAGER_GROUP, remove=remove)

def _set_group_membership(email, group, remove=False):
    api = AuthentikAPI()
    user = _get_user(api, email)
    groups = api.get('/core/groups/', params={'name': group})['results']
    if not groups:
        raise RuntimeError(
            f"group {group} not found — has authentik applied the ycluster blueprint?")
    action = 'remove_user' if remove else 'add_user'
    api.post(f"/core/groups/{groups[0]['pk']}/{action}/", json={'pk': user['pk']})
    return f"{'removed' if remove else 'added'} {email} {'from' if remove else 'to'} {group}"

def set_active(email, active):
    """Enable or disable an account (is_active in authentik). Disabling
    blocks login without removing the account."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    api.patch(f"/core/users/{user['pk']}/", json={'is_active': active})
    return f"{'enabled' if active else 'disabled'} {email}"

def update_user(email, name=None, project_name=None, admin_notes=None):
    """Update mutable user fields. Only provided fields are changed;
    a field passed as None is skipped (so CLI can pass only what was set).
    project_name and admin_notes are stored in authentik's user attributes
    JSON; pass '' to clear, a string to set."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    payload = {}
    if name is not None:
        payload['name'] = name
    if project_name is not None or admin_notes is not None:
        # Read-modify-write on attributes: we only own the keys we set
        # (project_name, admin_notes), so merging preserves anything
        # authentik or other tools have written.
        attrs = dict(user.get('attributes') or {})
        if project_name is not None:
            attrs['project_name'] = project_name
        if admin_notes is not None:
            attrs['admin_notes'] = admin_notes
        payload['attributes'] = attrs
    if not payload:
        raise RuntimeError("no updates specified")
    api.patch(f"/core/users/{user['pk']}/", json=payload)
    return f"updated {email}"

def recovery_link(email):
    """Generate a one-time password-set link for an existing account
    (out-of-band delivery — no SMTP anywhere in the cluster)."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    result = api.post(f"/core/users/{user['pk']}/recovery/", json={})
    # Re-root the link on the user-facing base — the API builds it from the
    # request host (cluster-internal).
    link = result['link']
    return _link_base_url() + '/if/' + link.split('/if/', 1)[1]

def revoke_invitation(email):
    """Delete outstanding invitation(s) for an email. Returns the count."""
    api = AuthentikAPI()
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    matched = [inv for inv in invitations
               if (inv.get('fixed_data') or {}).get('email') == email]
    for inv in matched:
        api._request('DELETE', f"/stages/invitation/invitations/{inv['pk']}/")
    return len(matched)

def _matched_invitations(api, email):
    """Invitation records whose fixed_data targets `email`."""
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    return [inv for inv in invitations
            if (inv.get('fixed_data') or {}).get('email') == email]


def _invitation_project_result(email, project, count):
    kind = 'tagged' if project else 'cleared project tag from'
    suffix = 'with project {project!r}' if project else ''
    plural = 's' if count != 1 else ''
    if project:
        return f"tagged {count} invitation{plural} for {email} with project {project!r}"
    return f"cleared project tag from {count} invitation{plural} for {email}"


def set_invitation_project(email, project):
    """Tag an outstanding invitation for `email` with `project`, stored in
    the invitation's fixed_data (the enrollment flow's user-write stage
    writes it onto the enrolled account's project_name). Pass `project` as
    '' to clear the tag."""
    api = AuthentikAPI()
    matched = _matched_invitations(api, email)
    if not matched:
        raise RuntimeError(f"no outstanding invitation for {email}")
    for inv in matched:
        fixed = dict(inv.get('fixed_data') or {})
        if project:
            fixed['project_name'] = project
        else:
            fixed.pop('project_name', None)
        api.patch(f"/stages/invitation/invitations/{inv['pk']}/",
                  json={'fixed_data': fixed})
    return _invitation_project_result(email, project, len(matched))
