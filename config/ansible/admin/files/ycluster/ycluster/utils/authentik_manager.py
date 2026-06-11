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

import requests

from ..common.etcd_utils import get_etcd_client

ENROLLMENT_FLOW_SLUG = 'enrollment-invitation'
TOKEN_ETCD_KEY = '/cluster/config/authentik/bootstrap-token'
ADMIN_GROUP = 'ycluster-admins'


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

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, self.base + path, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"authentik API {method} {path}: {resp.status_code} {resp.text}")
        return resp.json() if resp.text else None

    def get(self, path, **kwargs):
        return self._request('GET', path, **kwargs)

    def post(self, path, json):
        return self._request('POST', path, json=json)

    def flow_pk(self, slug):
        flows = self.get('/flows/instances/', params={'slug': slug})['results']
        if not flows:
            raise RuntimeError(f"flow '{slug}' not found — is the enrollment blueprint applied?")
        return flows[0]['pk']


def _get_user(api, email):
    users = api.get('/core/users/', params={'username': email})['results']
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


def invite_user(email, name=None, days=7):
    """Issue a single-use enrollment invitation pre-bound to the email.
    Returns the invitation URL."""
    api = AuthentikAPI()
    # Enrollment always creates the account, so an invitation for an
    # existing account would fail at the user_write stage. Refuse early.
    existing = api.get('/core/users/', params={'username': email})['results']
    if existing:
        raise RuntimeError(
            f"account {email} already exists — enrollment invitations are for "
            f"new accounts. Issue a password (re)set link instead "
            f"('ycluster user recovery {email}').")
    invitation = api.post('/stages/invitation/invitations/', json={
        'name': 'invite-' + email.replace('@', '-at-').replace('.', '-'),
        'flow': api.flow_pk(ENROLLMENT_FLOW_SLUG),
        'single_use': True,
        'expires': (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        'fixed_data': {
            'username': email,
            'email': email,
            'name': name or email,
        },
    })
    return f"{_link_base_url()}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={invitation['pk']}"


def users_data():
    """Accounts as a list of dicts (email, name, type, active, last_login,
    is_admin), service accounts excluded."""
    api = AuthentikAPI()
    users = api.get('/core/users/', params={'page_size': 500})['results']
    rows = []
    for u in users:
        if u['type'] not in ('internal', 'external'):
            continue
        group_names = [g.get('name') for g in (u.get('groups_obj') or [])]
        rows.append({
            'email': u['email'] or u['username'],
            'name': u['name'],
            'type': u['type'],
            'active': bool(u['is_active']),
            'last_login': u['last_login'],
            'is_admin': ADMIN_GROUP in group_names,
        })
    return sorted(rows, key=lambda r: r['email'])


def invitations_data():
    """Outstanding invitations as a list of dicts (email, expires, url)."""
    api = AuthentikAPI()
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    link_base = _link_base_url()
    return [{
        'email': (inv.get('fixed_data') or {}).get('email', inv['name']),
        'expires': inv['expires'],
        'url': f"{link_base}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={inv['pk']}",
    } for inv in invitations]


def set_admin(email, remove=False):
    """Add (or remove) an account to the ycluster-admins group, which gates
    the forward-auth'd admin web pages. Returns the action performed."""
    api = AuthentikAPI()
    user = _get_user(api, email)
    groups = api.get('/core/groups/', params={'name': ADMIN_GROUP})['results']
    if not groups:
        raise RuntimeError(
            f"group {ADMIN_GROUP} not found — has authentik applied the admin blueprint?")
    action = 'remove_user' if remove else 'add_user'
    api.post(f"/core/groups/{groups[0]['pk']}/{action}/", json={'pk': user['pk']})
    return f"{'removed' if remove else 'added'} {email} {'from' if remove else 'to'} {ADMIN_GROUP}"


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
