"""
LiteLLM custom auth hook: validates API keys against Open-WebUI's PostgreSQL database.

This allows users to use the same API key they generate in Open-WebUI's Account
Settings for direct inference access at inference.xc, OpenCode, curl, etc.

The master key (LITELLM_MASTER_KEY env var) always bypasses this check — it is
used internally by Open-WebUI to call LiteLLM as a backend.

LiteLLM calls this function for every request. It must return a UserAPIKeyAuth
object on success or raise an Exception to reject the request.

Config reference in litellm-config.yaml:
  general_settings:
    custom_auth: litellm_custom_auth.user_api_key_auth
"""

import hashlib
import os
import asyncpg
from fastapi import Request
from litellm.proxy._types import UserAPIKeyAuth

# Connection pools — created on first use
_owui_pool = None
_litellm_pool = None


async def _get_owui_pool():
    global _owui_pool
    if _owui_pool is None:
        dsn = os.environ.get("OPENWEBUI_DATABASE_URL")
        if not dsn:
            raise RuntimeError("OPENWEBUI_DATABASE_URL is not set")
        _owui_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _owui_pool


async def _get_litellm_pool():
    global _litellm_pool
    if _litellm_pool is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        # Strip query params that Prisma may add (e.g. connection_limit)
        dsn = dsn.split("?")[0]
        _litellm_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _litellm_pool


async def _lookup_openwebui_key(api_key: str):
    """
    Look up an API key in Open-WebUI's api_key table.
    Returns (user_id, email, role) if found and valid, else None.
    """
    pool = await _get_owui_pool()
    row = await pool.fetchrow(
        """
        SELECT ak.user_id, u.email, u.role
        FROM api_key ak
        JOIN "user" u ON u.id = ak.user_id
        WHERE ak.key = $1
        """,
        api_key,
    )
    return row  # asyncpg.Record or None


async def _lookup_litellm_key(api_key: str):
    """
    Look up a key in LiteLLM's verification token table.
    LiteLLM stores keys as SHA-256 hashes.
    Returns True if found (valid LiteLLM-internal key), else False.
    """
    hashed = hashlib.sha256(api_key.encode()).hexdigest()
    pool = await _get_litellm_pool()
    row = await pool.fetchrow(
        'SELECT token FROM "LiteLLM_VerificationToken" WHERE token = $1',
        hashed,
    )
    return row is not None


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    """
    Custom auth handler for LiteLLM.

    Called for every request including master key requests. We must explicitly
    allow the master key through, then validate user keys against Open-WebUI.

    The LiteLLM UI authenticates with JWT tokens after login — these don't
    start with "sk-" and must be allowed through as admin.
    """
    if not api_key:
        raise Exception("Missing API key")

    # Allow master key through — LiteLLM does NOT skip custom auth for it
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if master_key and api_key == master_key:
        return UserAPIKeyAuth(
            api_key=api_key,
            user_role="proxy_admin",
        )

    # Non-sk- tokens (e.g. JWT) — let them through as admin.
    # The LiteLLM UI uses JWTs after /v2/login.
    if not api_key.startswith("sk-"):
        return UserAPIKeyAuth(
            api_key=api_key,
            user_role="proxy_admin",
        )

    # Check if this is a LiteLLM-internal key (e.g. UI session key generated
    # by /v2/login). These are sk- prefixed but stored in LiteLLM's own DB.
    if await _lookup_litellm_key(api_key):
        return UserAPIKeyAuth(
            api_key=api_key,
            user_role="proxy_admin",
        )

    # Finally, check Open-WebUI's api_key table for user keys
    row = await _lookup_openwebui_key(api_key)

    if row is None:
        raise Exception("Invalid API key")

    return UserAPIKeyAuth(
        api_key=api_key,
        user_id=row["user_id"],
        user_email=row["email"],
        key_alias=row["email"],
        # Map Open-WebUI roles to LiteLLM roles
        user_role="proxy_admin" if row["role"] == "admin" else "internal_user",
    )
