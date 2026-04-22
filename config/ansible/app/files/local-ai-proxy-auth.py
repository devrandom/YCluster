#!/usr/bin/env python3
"""
local-ai-proxy auth validator.

A tiny HTTP service intended to be called by nginx auth_request. Given
a Bearer token on the incoming request, returns 200 with X-User-Id
set, or 401. nginx forwards X-User-Id to local-ai-proxy, which trusts
it because the request comes from loopback.

Validation sources (in order):
  1. LiteLLM master key from etcd /cluster/config/litellm/master-key
     → user = "root"
  2. Open-WebUI's api_key table → user = u.email

Listens on 127.0.0.1:4002 by default (override with LISTEN_ADDR env).
"""

import logging
import os
import sys
from wsgiref.simple_server import make_server

import etcd3
import psycopg2
from flask import Flask, Response, request


MASTER_KEY_ETCD_PATH = "/cluster/config/litellm/master-key"
OWUI_DSN = "dbname=openwebui user=openwebui password=openwebui host=localhost"

app = Flask(__name__)
log = logging.getLogger("auth-validator")

_state = {
    "master_key": None,   # lazily loaded from etcd
    "pg": None,           # psycopg2 connection, reconnect on failure
}


def get_master_key():
    if _state["master_key"] is None:
        client = etcd3.client()
        value, _ = client.get(MASTER_KEY_ETCD_PATH)
        if value:
            _state["master_key"] = value.decode().strip()
    return _state["master_key"]


def get_pg():
    conn = _state["pg"]
    if conn is None or conn.closed:
        conn = psycopg2.connect(OWUI_DSN)
        _state["pg"] = conn
    return conn


def lookup_openwebui_user(api_key):
    """Return email for an api_key row, or None."""
    try:
        conn = get_pg()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.email FROM api_key ak
                JOIN "user" u ON u.id = ak.user_id
                WHERE ak.key = %s
                """,
                (api_key,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except psycopg2.Error as e:
        log.warning("openwebui lookup failed: %s", e)
        # Force reconnect on next call.
        try:
            if _state["pg"]:
                _state["pg"].close()
        except Exception:
            pass
        _state["pg"] = None
        return None


@app.route("/auth")
def auth():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return Response(status=401)
    token = auth_header[len("bearer "):].strip()
    if not token:
        return Response(status=401)

    master = get_master_key()
    if master and token == master:
        resp = Response(status=200)
        resp.headers["X-User-Id"] = "root"
        return resp

    email = lookup_openwebui_user(token)
    if email:
        resp = Response(status=200)
        resp.headers["X-User-Id"] = email
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
