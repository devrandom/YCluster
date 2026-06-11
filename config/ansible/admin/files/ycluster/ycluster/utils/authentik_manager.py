"""
Account management against the cluster IdP (authentik).

Authenticates with the bootstrap API token that authentik-manager
generates once into etcd. The base URL defaults to the cluster-internal
nginx vhost; override with YC_AUTHENTIK_URL (e.g. http://127.0.0.1:9300
when running on the storage leader without DNS).
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from ..common.etcd_utils import get_etcd_client

ENROLLMENT_FLOW_SLUG = 'enrollment-invitation'
TOKEN_ETCD_KEY = '/cluster/config/authentik/bootstrap-token'


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


def add_user(email, name=None):
    """Create an internal account keyed by email (no credentials — for
    external-login linking; use invite for internal-password onboarding)."""
    api = AuthentikAPI()
    user = api.post('/core/users/', json={
        'username': email,
        'email': email,
        'name': name or email,
        'type': 'internal',
        'is_active': True,
    })
    print(f"Created user {user['username']} (pk {user['pk']})")


def invite_user(email, name=None, days=7):
    """Issue a single-use enrollment invitation pre-bound to the email."""
    api = AuthentikAPI()
    # Enrollment always creates the account, so an invitation for an
    # existing account would fail at the user_write stage. Refuse early.
    existing = api.get('/core/users/', params={'username': email})['results']
    if existing:
        raise RuntimeError(
            f"account {email} already exists — enrollment invitations are for "
            f"new accounts. Use 'ycluster user recovery {email}' to issue a "
            f"password (re)set link instead.")
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
    url = f"{_link_base_url()}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={invitation['pk']}"
    print(f"Invitation for {email} (expires in {days}d, single use):")
    print(f"  {url}")


def list_users():
    api = AuthentikAPI()
    users = api.get('/core/users/', params={'page_size': 500})['results']
    fmt = "{:<40} {:<30} {:<10} {:<8} {}"
    print(fmt.format('EMAIL', 'NAME', 'TYPE', 'ACTIVE', 'LAST LOGIN'))
    for u in users:
        if u['type'] not in ('internal', 'external'):
            continue
        print(fmt.format(u['email'] or u['username'], u['name'], u['type'],
                         'yes' if u['is_active'] else 'no',
                         u['last_login'] or 'never'))


def recovery_link(email):
    """Generate a one-time password-set link for an existing account
    (out-of-band delivery — no SMTP anywhere in the cluster)."""
    api = AuthentikAPI()
    users = api.get('/core/users/', params={'username': email})['results']
    if not users:
        raise RuntimeError(f"no account for {email}")
    result = api.post(f"/core/users/{users[0]['pk']}/recovery/", json={})
    # Re-root the link on the user-facing base — the API builds it from the
    # request host (cluster-internal).
    link = result['link']
    link = _link_base_url() + '/if/' + link.split('/if/', 1)[1]
    print(f"Password (re)set link for {email} (one-time):")
    print(f"  {link}")


def revoke_invitation(email):
    """Delete outstanding invitation(s) for an email."""
    api = AuthentikAPI()
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    matched = [inv for inv in invitations
               if (inv.get('fixed_data') or {}).get('email') == email]
    if not matched:
        print(f"No outstanding invitation for {email}")
        return
    for inv in matched:
        api._request('DELETE', f"/stages/invitation/invitations/{inv['pk']}/")
        print(f"Revoked invitation {inv['pk']} for {email}")


def list_invitations():
    api = AuthentikAPI()
    invitations = api.get('/stages/invitation/invitations/', params={'page_size': 500})['results']
    fmt = "{:<40} {:<28} {}"
    print(fmt.format('EMAIL', 'EXPIRES', 'URL'))
    link_base = _link_base_url()
    for inv in invitations:
        email = (inv.get('fixed_data') or {}).get('email', inv['name'])
        url = f"{link_base}/if/flow/{ENROLLMENT_FLOW_SLUG}/?itoken={inv['pk']}"
        print(fmt.format(email, inv['expires'] or '-', url))
