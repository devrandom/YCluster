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

Listens on 127.0.0.1:4002 by default (override with LISTEN_ADDR env).
"""

import json
import logging
import os
import secrets
import threading
import time
from wsgiref.simple_server import WSGIServer, make_server
from socketserver import ThreadingMixIn

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
    token_digest,
)
from ycluster.common.etcd_utils import get_etcd_client

MASTER_KEY_ETCD_PATH = "/cluster/config/inference/master-key"

app = Flask(__name__)
log = logging.getLogger("auth-validator")

_state = {
    "master_key": None,   # lazily loaded from etcd
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


class UserStore:
    """In-memory snapshot of /cluster/users/<email>/keys and groups.

    Loaded once and refreshed on etcd watch events (hot-reload over the
    /cluster/users/ prefix) so per-request auth does no etcd round-trip.
    Only argon2id *hashes* and sha256 *digests* are held in memory — never
    plaintext tokens.
    """

    def __init__(self, client, hasher=None):
        self._client = client
        self._hasher = hasher or PasswordHasher()
        self._lock = threading.RLock()
        self._keys = {}     # email -> {key_id: argon2 hash}
        self._digests = {}  # email -> {key_id: sha256(token) or None (legacy)}
        self._groups = {}   # email -> [group names]
        self.reload()

    def reload(self):
        keys = {}
        digests = {}
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
                    # tail is "keys/<key_id>": KEYS_SUFFIX carries a leading
                    # slash that partition() already consumed, so measure the
                    # stripped prefix (using len(KEYS_SUFFIX) here silently
                    # drops the first character of every key id).
                    key_id = tail[len(KEYS_SUFFIX.lstrip("/")):]
                    if key_id and "/" not in key_id:
                        try:
                            data = json.loads(val.decode())
                            keys.setdefault(email, {})[key_id] = data.get("hash")
                            digests.setdefault(email, {})[key_id] = data.get("digest")
                        except (ValueError, TypeError):
                            continue
        except Exception as e:
            # A transient etcd failure shouldn't drop the last good snapshot.
            log.warning("failed to reload /cluster/users/ snapshot: %s", e)
            return
        with self._lock:
            self._keys = keys
            self._digests = digests
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
        """Return (email, groups) if `token` matches a stored hash, else None.

        Digest-indexed lookup first (sha256(token) -> one candidate record,
        then a single argon2 verify); legacy digest-less records still
        require an argon2 scan and are backfilled on first success."""
        if not token or not token.startswith(_TOKEN_PREFIX):
            return None
        digest = token_digest(token)
        with self._lock:
            for email, recs in self._digests.items():
                for key_id, d in recs.items():
                    if d != digest:
                        continue
                    if self._verify(self._keys[email][key_id], token):
                        return email, list(self._groups.get(email, []))
            for email, recs in self._keys.items():
                for key_id, h in recs.items():
                    if self._digests.get(email, {}).get(key_id):
                        continue
                    if self._verify(h, token):
                        self._backfill_digest(email, key_id, token)
                        return email, list(self._groups.get(email, []))
        return None

    def _verify(self, stored_hash, token):
        try:
            return self._hasher.verify(stored_hash, token)
        # Catch base names that exist across argon2 versions: on
        # Ubuntu's apt python3-argon2 (21.1.0) the names are
        # VerificationError + InvalidHash (no InvalidHashError),
        # while newer releases alias InvalidHash as
        # InvalidHashError. VerificationError covers the
        # VerifyMismatchError subclass on both.
        except (exceptions.InvalidHash, exceptions.VerificationError):
            return False

    def _backfill_digest(self, email, key_id, token):
        """Persist sha256(token) into a legacy record so future auths skip
        the argon2 scan. Best-effort: failure only costs performance."""
        try:
            path = USERS_PREFIX + email + KEYS_SUFFIX + key_id
            val, _ = self._client.get(path)
            if val is None:
                return
            data = json.loads(val.decode())
            if data.get("digest"):
                self._digests.setdefault(email, {})[key_id] = data["digest"]
                return
            data["digest"] = token_digest(token)
            self._client.put(path, json.dumps(data))
            self._digests.setdefault(email, {})[key_id] = data["digest"]
        except Exception as e:
            log.warning("digest backfill for %s/%s failed: %s", email, key_id, e)


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

    return Response(status=401)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Threaded WSGI server with a larger listen backlog.

    The stock make_server WSGI server is single-threaded (one request at a
    time) and binds the default backlog of 5. That serialized argon2-heavy
    /auth verifications ahead of the /healthz probes local-ai-proxy sends on
    every inference-health check; under load the probes blew their 3s client
    timeout and the proxy reported healthy auth as down. Threading lets a
    /healthz run in its own thread (the argon2 C verify releases the GIL), and
    the larger backlog lets a burst of probes queue instead of being dropped.
    """

    daemon_threads = True
    request_queue_size = 128


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    addr = os.environ.get("LISTEN_ADDR", "127.0.0.1:4002")
    host, port = addr.rsplit(":", 1)
    port = int(port)
    log.info("starting on %s:%d", host, port)
    srv = make_server(host, port, app, server_class=_ThreadingWSGIServer)
    with srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
