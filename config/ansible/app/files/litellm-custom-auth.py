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

import os
import asyncpg
from fastapi import Request
from litellm.proxy._types import UserAPIKeyAuth

# Connection pool — created on first use
_pool = None


async def _get_pool():
    global _pool
    if _pool is None:
        dsn = os.environ.get("OPENWEBUI_DATABASE_URL")
        if not dsn:
            raise RuntimeError("OPENWEBUI_DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pool


async def _lookup_key(api_key: str):
    """
    Look up an API key in Open-WebUI's api_key table.
    Returns (user_id, email, role) if found and valid, else None.
    """
    pool = await _get_pool()
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


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    """
    Custom auth handler for LiteLLM.

    Called for every request including master key requests. We must explicitly
    allow the master key through, then validate user keys against Open-WebUI.
    """
    if not api_key or not api_key.startswith("sk-"):
        raise Exception("Invalid API key format")

    # Allow master key through — LiteLLM does NOT skip custom auth for it
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if master_key and api_key == master_key:
        return UserAPIKeyAuth(
            api_key=api_key,
            user_role="proxy_admin",
        )

    row = await _lookup_key(api_key)

    if row is None:
        raise Exception("Invalid API key")

    return UserAPIKeyAuth(
        api_key=api_key,
        user_id=row["user_id"],
        user_email=row["email"],
        # Map Open-WebUI roles to LiteLLM roles
        user_role="proxy_admin" if row["role"] == "admin" else "internal_user",
    )
