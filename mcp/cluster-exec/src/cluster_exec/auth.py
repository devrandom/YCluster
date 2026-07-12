"""Bearer-token authentication for the cluster-exec MCP server.

The server requires an HTTP `Authorization: Bearer <token>` header on every
request. The token lives in a single file (mode 0600) at the path returned by
`default_token_path()` — by default `~/.config/cluster-exec/bearer`. The file
is created on first startup with a 256-bit random token and reused across
restarts so the token survives.
"""

import os
import secrets
import tempfile
from pathlib import Path

from mcp.server.auth.provider import AccessToken, TokenVerifier


def default_token_path() -> Path:
    """Return the bearer-token file location.

    Honors $CE_BEARER_PATH if set (with `~` expansion); otherwise uses
    `~/.config/cluster-exec/bearer`, matching the XDG_CONFIG_HOME convention.
    """
    override = os.environ.get("CE_BEARER_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "cluster-exec" / "bearer"


def load_or_create_token(path: Path) -> str:
    """Return the bearer token at `path`, generating+writing one if missing.

    Existing files are read verbatim (whitespace stripped). Missing files are
    created atomically (write to a sibling tempfile, chmod 0600, rename) with
    a fresh 256-bit random token from `secrets.token_urlsafe`.
    """
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd, tmp_name = tempfile.mkstemp(prefix=".bearer.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.write(fd, (token + "\n").encode())
        os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return token


class StaticTokenVerifier(TokenVerifier):
    """TokenVerifier that accepts exactly one bearer token (the file contents)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or token != self._token:
            return None
        return AccessToken(token=token, client_id="cluster-exec-local", scopes=[])
