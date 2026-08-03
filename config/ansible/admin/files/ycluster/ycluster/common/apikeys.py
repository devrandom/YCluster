"""Gateway API-key ownership.

The inference gateway authenticates per-user bearer tokens (`sk-...`)
against this etcd-backed store instead of Open-WebUI's `api_key` table.
Keys are owned by a cluster `user` object keyed by email and stored
hashed (argon2id, per-key salt); the plaintext exists only transiently
(minted once at create, or read once during migration).

etcd layout (the existing SSH-key blob is left untouched):
  /cluster/users/<email>               -> {"ssh_keys": [...]}        # existing
  /cluster/users/<email>/groups        -> ["group-a", "admin"]        # new
  /cluster/users/<email>/keys/<key-id> -> {"hash","created_at","label"}
"""

import json
import re
import secrets
import uuid
from datetime import datetime, timezone

from argon2 import PasswordHasher, exceptions

from .etcd_utils import get_etcd_client

USERS_PREFIX = "/cluster/users/"
GROUPS_SUFFIX = "/groups"
KEYS_SUFFIX = "/keys/"

_USER_RE = re.compile(r"^[A-Za-z0-9._%+@-]{1,128}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
_TOKEN_PREFIX = "sk-"


def _get(key):
    val, _ = get_etcd_client().get(key)
    return json.loads(val.decode()) if val else None


def _put(key, obj):
    get_etcd_client().put(key, json.dumps(obj))


def _valid_user(email):
    if not _USER_RE.match(email):
        raise ValueError(f"Invalid user '{email}': allowed characters are "
                         f"letters, digits and . _ % + @ -")


def _owners():
    """Iterate the emails that own data under /cluster/users/<email>/.

    The first path segment after the users prefix is always the email
    (the value record), its `/groups` child, or its `/keys/<id>` children.
    Dedupe across those; a bare value record (SSH keys only, no API keys)
    simply yields an owner with no API-key records, which callers handle
    as empty."""
    client = get_etcd_client()
    owners = set()
    for _val, meta in client.get_prefix(USERS_PREFIX):
        rest = meta.key.decode()[len(USERS_PREFIX):]
        email = rest.split("/", 1)[0]
        if email and email != "keys":
            owners.add(email)
    return sorted(owners)


def create_key(owner, groups=None, label=None, hasher=None):
    """Mint a new gateway API key owned by `owner` (email).

    Returns the plaintext `sk-...` exactly once; only the argon2id hash is
    persisted. Writes `owner`'s groups to /cluster/users/<email>/groups if
    provided. `hasher` is injectable for tests (defaults to real argon2).
    """
    _valid_user(owner)
    groups = [g for g in (groups or []) if g]
    secret = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    hasher = hasher or PasswordHasher()
    key_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    rec = {"hash": hasher.hash(secret),
           "created_at": now,
           "label": label or ""}
    _put(USERS_PREFIX + owner + KEYS_SUFFIX + key_id, rec)
    if groups:
        _put(USERS_PREFIX + owner + GROUPS_SUFFIX, groups)
    return secret


def list_keys(owner=None):
    """Return key metadata: {email: [{key_id, label, created_at}]}.

    Never returns the secret or the stored hash. If `owner` is given,
    returns only that owner's keys."""
    owners = [owner] if owner else _owners()
    out = {}
    client = get_etcd_client()
    for email in owners:
        _valid_user(email)
        prefix = USERS_PREFIX + email + KEYS_SUFFIX
        recs = []
        for val, meta in client.get_prefix(prefix):
            key_id = meta.key.decode()[len(prefix):]
            if not key_id or "/" in key_id:
                continue
            data = json.loads(val.decode())
            recs.append({"key_id": key_id,
                         "label": data.get("label", ""),
                         "created_at": data.get("created_at", "")})
        recs.sort(key=lambda r: r["created_at"])
        out[email] = recs
    return out


def revoke_key(key_id):
    """Idempotently remove a key by its key-id. Returns True if removed."""
    if not _KEY_ID_RE.match(key_id):
        raise ValueError(f"Invalid key id '{key_id}'")
    client = get_etcd_client()
    for owner in _owners():
        full = USERS_PREFIX + owner + KEYS_SUFFIX + key_id
        if client.get(full)[0] is not None:
            client.delete(full)
            return True
    return False


def validate_key(presented, hasher=None):
    """Return (email, groups) authenticating the presented token, or None.

    Iterates every stored key record and argon2-verifies the presented
    secret against each hash — intentionally not a plaintext lookup."""
    if not presented or not presented.startswith(_TOKEN_PREFIX):
        return None
    hasher = hasher or PasswordHasher()
    client = get_etcd_client()
    for owner in _owners():
        prefix = USERS_PREFIX + owner + KEYS_SUFFIX
        for val, _meta in client.get_prefix(prefix):
            data = json.loads(val.decode())
            try:
                if hasher.verify(data["hash"], presented):
                    groups = _get(USERS_PREFIX + owner + GROUPS_SUFFIX) or []
                    return owner, list(groups)
            except exceptions.VerifyMismatchError:
                continue
            except exceptions.InvalidHashError:
                continue
    return None


def _has_owner_token(email, plaintext, hasher):
    """True if an existing etcd key-record for `email` hashes to `plaintext`.

    Argon2 uses a per-key salt, so two hashes of the same plaintext differ;
    detecting an already-migrated token therefore means verifying the
    presented secret against each stored hash, exactly as the auth path
    does."""
    prefix = USERS_PREFIX + email + KEYS_SUFFIX
    for val, _meta in get_etcd_client().get_prefix(prefix):
        data = json.loads(val.decode())
        try:
            if hasher.verify(data["hash"], plaintext):
                return True
        except (exceptions.VerifyMismatchError, exceptions.InvalidHashError):
            continue
    return False


def migrate_owui_keys(hasher=None, owui_dsn=None):
    """Copy Open-WebUI API keys into the etcd store, re-hashed in place.

    Reads OWUI's api_key/user/group_member/group tables (the same join
    the legacy validator used), and for each row writes an etcd key record
    holding the argon2id hash of the existing `sk-...` value together with
    the owner's groups. The plaintext is never written to etcd, so the
    user keeps their same token — no rotation.

    Idempotent: a token that already exists for its owner (whose stored
    hash verifies) is skipped instead of minting a duplicate, so re-running
    after an interrupted or partial run only backfills what's missing.

    Returns a count of keys newly copied."""
    psycopg2 = _import_psycopg2()
    dsn = owui_dsn or "dbname=openwebui user=openwebui password=openwebui host=localhost"
    hasher = hasher or PasswordHasher()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    count = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ak.key, u.email, u.role,
                       COALESCE(
                           array_agg(g.name) FILTER (WHERE g.name IS NOT NULL),
                           '{}'
                       ) AS groups
                FROM api_key ak
                JOIN "user" u ON u.id = ak.user_id
                LEFT JOIN group_member gm ON gm.user_id = u.id
                LEFT JOIN "group" g ON g.id = gm.group_id
                GROUP BY ak.key, u.email, u.role
                """
            )
            for plaintext, email, role, groups in cur.fetchall():
                groups = list(groups or [])
                if role == "admin" and "admin" not in groups:
                    groups.append("admin")
                _valid_user(email)
                if _has_owner_token(email, plaintext, hasher):
                    # Already migrated under a previous key-id; just keep the
                    # owner's groups reconciled.
                    if groups:
                        _put(USERS_PREFIX + email + GROUPS_SUFFIX, groups)
                    continue
                key_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat()
                rec = {"hash": hasher.hash(plaintext),
                       "created_at": now}
                _put(USERS_PREFIX + email + KEYS_SUFFIX + key_id, rec)
                if groups:
                    _put(USERS_PREFIX + email + GROUPS_SUFFIX, groups)
                count += 1
    finally:
        conn.close()
    return count


def _import_psycopg2():
    import importlib
    try:
        return importlib.import_module("psycopg2")
    except ModuleNotFoundError:
        raise RuntimeError("psycopg2 is required for `ycluster inference key migrate`")
