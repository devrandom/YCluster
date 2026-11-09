#!/usr/bin/env python3
"""Finish Receipt Wrangler's first-boot configuration over its own API.

The app creates a bootstrap admin/admin account whenever the users table is
empty. This script rotates that account to the etcd-stored password and, in
full mode (external access), points the app at its public URL and registers
the Authentik OIDC provider.

Idempotent: a rotated password, an existing server URL, or an existing
"authentik" provider are all treated as success. Run as
``wrangler-bootstrap --rotate-only`` from wrangler-manager on every start.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


API = os.environ.get("WRANGLER_API_URL", "http://127.0.0.1:9082")
ADMIN_PASSWORD = os.environ["WRANGLER_ADMIN_PASSWORD"]
PUBLIC_URL = os.environ.get("WRANGLER_SERVER_PUBLIC_URL", "")
ISSUER = os.environ.get("WRANGLER_OIDC_ISSUER", "")
OIDC_SECRET = os.environ.get("WRANGLER_OIDC_CLIENT_SECRET", "")
# URL slug for the provider inside the app (also the /api/oidc/<slug>/ path),
# distinct from the OAuth client_id registered at Authentik.
PROVIDER = "authentik"
CLIENT_ID = os.environ.get("WRANGLER_OIDC_CLIENT_ID", "wrangler")


def call(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return error.code, None
    except urllib.error.URLError as error:
        print(f"wrangler-bootstrap: cannot reach {API}: {error}", file=sys.stderr)
        sys.exit(1)


def login(password):
    status, body = call(
        "POST", "/api/login/?tokensInBody=true",
        {"username": "admin", "password": password},
    )
    if status != 200 or not body or not body.get("jwt"):
        return None
    return body["jwt"]


def wait_for_login(timeout=180):
    """Wait until the API answers, then report which password it accepts.

    The app seeds roles and the bootstrap admin after the HTTP listener is
    already up, so an early login attempt fails transiently. Returns
    "default" (admin/admin still active), "rotated" (etcd password works),
    or exits non-zero if neither ever works.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if login("admin") is not None:
            return "default"
        if login(ADMIN_PASSWORD) is not None:
            return "rotated"
        time.sleep(3)
    raise SystemExit("wrangler-bootstrap: API did not accept either admin password")


def rotate_default_admin():
    if wait_for_login() != "default":
        return
    token = login("admin")
    status, users = call("GET", "/api/user/", token=token)
    if status != 200 or not isinstance(users, list):
        raise SystemExit("wrangler-bootstrap: cannot list users")
    admin = next((u for u in users if u.get("username") == "admin"), None)
    if admin is None:
        raise SystemExit("wrangler-bootstrap: bootstrap admin account not found")
    status, _ = call(
        "POST", f"/api/user/{admin['id']}/resetPassword",
        {"password": ADMIN_PASSWORD}, token=token,
    )
    if status not in (200, 204):
        raise SystemExit(f"wrangler-bootstrap: password reset failed ({status})")
    print("wrangler-bootstrap: rotated the bootstrap admin password")


def set_public_url(token):
    status, settings = call("GET", "/api/systemSettings/", token=token)
    if status != 200 or not isinstance(settings, dict):
        raise SystemExit("wrangler-bootstrap: cannot read system settings")
    if settings.get("serverPublicUrl") == PUBLIC_URL:
        return
    settings["serverPublicUrl"] = PUBLIC_URL
    status, _ = call("PUT", "/api/systemSettings/", settings, token=token)
    if status not in (200, 204):
        raise SystemExit(f"wrangler-bootstrap: system settings update failed ({status})")
    print(f"wrangler-bootstrap: server public URL set to {PUBLIC_URL}")


def create_provider(token):
    # Create-and-tolerate is deliberate: the paged provider list endpoint
    # answers 500 on this build, so the create response is the only reliable
    # existence check. A duplicate name is a 400 validator error on "name".
    status, body = call(
        "POST", "/api/oidcProvider/",
        {
            "name": PROVIDER,
            "displayName": "Authentik",
            "issuerUrl": ISSUER,
            "clientId": CLIENT_ID,
            "clientSecret": OIDC_SECRET,
            "scope": "openid profile email",
            "allowProvisioning": True,
            "linkByUsername": False,
            "enabled": True,
        },
        token=token,
    )
    if status in (200, 201, 204):
        print(f"wrangler-bootstrap: registered OIDC provider {PROVIDER} ({ISSUER})")
        return
    if status == 400 and body and "already exists" in json.dumps(body):
        return
    raise SystemExit(
        f"wrangler-bootstrap: OIDC provider creation failed ({status}): {body}")


def main():
    rotate_default_admin()
    if "--rotate-only" in sys.argv:
        return
    if not PUBLIC_URL or not ISSUER or not OIDC_SECRET:
        return
    token = login(ADMIN_PASSWORD)
    if token is None:
        raise SystemExit("wrangler-bootstrap: admin login failed after rotation")
    set_public_url(token)
    create_provider(token)


if __name__ == "__main__":
    main()
