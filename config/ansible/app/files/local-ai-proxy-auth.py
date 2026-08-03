#!/usr/bin/env python3
"""
local-ai-proxy auth validator.

A tiny HTTP service intended to be called by nginx auth_request. Given
a Bearer token on the incoming request, returns 200 with X-User-Id
and X-User-Groups set, or 401. nginx forwards both headers to
local-ai-proxy, which trusts them because the request comes from
loopback.

Validation sources (in order):
  1. Cluster admin master key from etcd /cluster/config/inference/master-key
     → user = "root", groups = ""
  2. Per-user gateway API keys from etcd /cluster/users/<email>/keys/ ...
     → user = <email>, groups = values read from
       /cluster/users/<email>/groups. This store is hot-reloaded on the
       /cluster/users/ prefix so per-request auth does no etcd round-trip.
  3. (Optional migration fallback) Open-WebUI's api_key table → user =
     u.email, groups = comma-separated OWUI group names. Only consulted
     when auth.allow_owui_fallback is true in /etc/local-ai-proxy/config.yaml
     (default false), and only when the etcd lookup misses. This lets an
     existing cluster roll out the new validator while keys are copied
     over, then be disabled and dropped.

Listens on 127.0.0.1:4002 by default (override with LISTEN_ADDR env).
"""

import json
import logging
import os
import secrets
import threading
import time
from wsgiref.simple_server import make_server

import yaml
from argon2 import PasswordHasher, exceptions
from flask import Flask, Response, request

# ycluster is pip-installed on the storage nodes this runs on; reuse the single
# TLS-aware etcd client (reads ETCD_HOSTS + ETCD_CACERT/CERT/KEY from env)
# rather than hardcoding a plaintext localhost connection.
from ycluster.common.apikeys import (
    GROUPS_SUFFIX,
    KEYS_SUFFIX,
    USERS_PREFIX,
    _TOKEN_PREFIX,
)
from ycluster.common.etcd_utils import get_etcd_client

MASTER_KEY_ETCD_PATH = "/cluster/config/inference/master-key"
PROXY_CONFIG_PATH = "/etc/local-ai-proxy/config.yaml"
OWUI_DSN = "dbname=openwebui user=openwebui password=openwebui host=localhost"

app = Flask(__name__)
log = logging.getLogger("auth-validator")

_state = {
    "master_key": None,   # lazily loaded from etcd
    "pg": None,           # psycopg2 connection, reconnect on failure
    "store": None,        # UserStore snapshot, lazily built
}
_store_lock = threading.RLock()


def get_master_key():
    if _state["master_key"] is None:
        client = get_etcd_client()
        value, _ = client.get(MASTER_KEY_ETCD_PATH)
        if value:
            _state["master_key"] = value.decode().strip()
    return _state["master_key"]


def load_allow_owui_fallback(path=None):
    """Read auth.allow_owui_fallback from the proxy config (default false).

    The flag lives in the Ansible-managed YAML so enabling the migration
    window is a playbook change, not a redeploy. Missing key/file → false.
    """
    if path is None:
        path = PROXY_CONFIG_PATH
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except OSError:
        return False
    return bool((cfg.get("auth") or {}).get("allow_owui_fallback", False))


class UserStore:
    """In-memory snapshot of /cluster/users/<email>/keys and groups.

    Loaded once and refreshed on etcd watch events (hot-reload over the
    /cluster/users/ prefix) so per-request auth does no etcd round-trip.
    Only argon2id *hashes* are held in memory — never plaintext tokens.
    """

    def __init__(self, client, hasher=None):
        self._client = client
        self._hasher = hasher or PasswordHasher()
        self._lock = threading.RLock()
        self._keys = {}    # email -> {key_id: hash}
        self._groups = {}  # email -> [group names]
        self.reload()

    def reload(self):
        keys = {}
        groups = {}
        try:
            for val, meta in self._client.get_prefix(USERS_PREFIX):
                key = meta.key.decode()
                rest = key[len(USERS_PREFIX):]
                email, _, tail = rest.partition("/")
                if not email:
                    continue
                if tail == GROUPS_SUFFIX.lstrip("/"):
                    try:
                        groups[email] = json.loads(val.decode())
                    except (ValueError, TypeError):
                        continue
                elif tail.startswith(KEYS_SUFFIX.lstrip("/")):
                    key_id = tail[len(KEYS_SUFFIX):]
                    if key_id and "/" not in key_id:
                        try:
                            data = json.loads(val.decode())
                            keys.setdefault(email, {})[key_id] = data.get("hash")
                        except (ValueError, TypeError):
                            continue
        except Exception as e:
            # A transient etcd failure shouldn't drop the last good snapshot.
            log.warning("failed to reload /cluster/users/ snapshot: %s", e)
            return
        with self._lock:
            self._keys = keys
            self._groups = groups

    def start_watch(self):
        """Begin hot-reloading the snapshot on /cluster/users/ changes."""

        def _watch():
            while True:
                try:
                    it, _cancel = self._client.watch_prefix(USERS_PREFIX)
                    for _event in it:
                        self.reload()
                except Exception as e:
                    log.warning("etcd watch on %s failed: %s; retrying", USERS_PREFIX, e)
                    time.sleep(1)

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def auth_user(self, token):
        """Return (email, groups) if `token` matches a stored hash, else None."""
        if not token or not token.startswith(_TOKEN_PREFIX):
            return None
        with self._lock:
            for email, recs in self._keys.items():
                for _key_id, h in recs.items():
                    try:
                        if self._hasher.verify(h, token):
                            return email, list(self._groups.get(email, []))
                    # Catch base names that exist across argon2 versions: on
                    # Ubuntu's apt python3-argon2 (21.1.0) the names are
                    # VerificationError + InvalidHash (no InvalidHashError),
                    # while newer releases alias InvalidHash as
                    # InvalidHashError. VerificationError covers the
                    # VerifyMismatchError subclass on both.
                    except (exceptions.InvalidHash, exceptions.VerificationError):
                        continue
        return None


def get_store():
    with _store_lock:
        if _state["store"] is None:
            store = UserStore(get_etcd_client())
            store.start_watch()
            _state["store"] = store
        return _state["store"]


def reset_store(store=None):
    """Replace/clear the cached store (test seam)."""
    with _store_lock:
        _state["store"] = store


def get_pg():
    conn = _state["pg"]
    if conn is None or conn.closed:
        import psycopg2
        conn = psycopg2.connect(OWUI_DSN)
        conn.autocommit = True
        _state["pg"] = conn
    return conn


def lookup_openwebui_user(api_key):
    """Return (email, groups) for an api_key row, or (None, []).

    groups is a list of OWUI group names the user is a member of. Only
    used during the migration window when auth.allow_owui_fallback is on.
    """
    try:
        conn = get_pg()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.email, u.role,
                       COALESCE(
                           array_agg(g.name) FILTER (WHERE g.name IS NOT NULL),
                           '{}'
                       ) AS groups
                FROM api_key ak
                JOIN "user" u ON u.id = ak.user_id
                LEFT JOIN group_member gm ON gm.user_id = u.id
                LEFT JOIN "group" g ON g.id = gm.group_id
                WHERE ak.key = %s
                GROUP BY u.email, u.role
                """,
                (api_key,),
            )
            row = cur.fetchone()
            if row is None:
                return None, []
            email, role, groups = row
            groups = list(groups or [])
            if role == "admin":
                groups.append("admin")
            return email, groups
    except Exception as e:
        log.warning("openwebui lookup failed: %s", e)
        # Force reconnect on next call.
        try:
            if _state["pg"]:
                _state["pg"].close()
        except Exception:
            pass
        _state["pg"] = None
        return None, []


@app.route("/auth")
def auth():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return Response(status=401)
    token = auth_header[len("bearer "):].strip()
    if not token:
        return Response(status=401)

    master = get_master_key()
    # compare_digest: constant-time (no early exit on first mismatch, so
    # response timing can't leak key prefixes). Bytes, not str: the str form
    # raises TypeError on non-ASCII, and token is attacker-controlled.
    if master and secrets.compare_digest(token.encode(), master.encode()):
        resp = Response(status=200)
        resp.headers["X-User-Id"] = "root"
        resp.headers["X-User-Groups"] = ""
        return resp

    user = get_store().auth_user(token)
    if user:
        email, groups = user
        resp = Response(status=200)
        resp.headers["X-User-Id"] = email
        resp.headers["X-User-Groups"] = ",".join(groups)
        return resp

    if load_allow_owui_fallback():
        email, groups = lookup_openwebui_user(token)
        if email:
            # Distinguishes the migration fallback path from a normal etcd
            # hit so ops can watch for the last user still authing via OWUI
            # and flip allow_owui_fallback off once it goes quiet.
            log.info("auth via owui fallback: %s", email)
            resp = Response(status=200)
            resp.headers["X-User-Id"] = email
            resp.headers["X-User-Groups"] = ",".join(groups)
            return resp

    return Response(status=401)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    addr = os.environ.get("LISTEN_ADDR", "127.0.0.1:4002")
    host, port = addr.rsplit(":", 1)
    port = int(port)
    log.info("starting on %s:%d", host, port)
    with make_server(host, port, app) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
