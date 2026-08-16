import asyncio
import json
import math
import os
import secrets
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import TextContent
from pydantic import AnyHttpUrl, ConfigDict, create_model
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import auth, exec, log, ratelimit, validate


# All tools must reject unknown arguments. The Pydantic default is
# `extra="ignore"`, which silently drops unknown keys (and lets typos like
# `yield_time_ms` masquerade as no-ops). We rebuild each tool's arg model on
# a base that sets `extra="forbid"` and also stamp `additionalProperties: false`
# on the advertised JSON schema so spec-compliant MCP clients reject unknown
# keys before sending.
_STRICT_ARG_BASE = type(
    "StrictArgModelBase",
    (ArgModelBase,),
    {"model_config": ConfigDict(arbitrary_types_allowed=True, extra="forbid")},
)


def _lock_down_tool_schemas() -> None:
    for tool in SERVER._tool_manager.list_tools():
        tool.parameters["additionalProperties"] = False
        old = tool.fn_metadata.arg_model
        new = create_model(
            old.__name__,
            __base__=_STRICT_ARG_BASE,
            **{
                name: (field.annotation, field)
                for name, field in old.model_fields.items()
            },
        )
        tool.fn_metadata.arg_model = new


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_HOST = os.environ.get("CE_HTTP_HOST", "127.0.0.1")
_PORT = _env_int("CE_HTTP_PORT", 4003)
_URL_BASE = f"http://{_HOST}:{_PORT}"

_BEARER_PATH = auth.default_token_path()
_BEARER_TOKEN = auth.load_or_create_token(_BEARER_PATH)

SERVER = MCPServer(
    "cluster-exec",
    instructions=(
        "Six tools: `run` executes shell commands on internal hosts via "
        "SSH through a bastion (grants the configured SSH user full remote "
        "code execution \u2014 use sparingly); `put` uploads a local file "
        "to a target host via SSH (binary-safe, foreground only); `get` "
        "downloads a remote file from a target host to the local "
        "filesystem via SSH (binary-safe, foreground only); `tail` "
        "renders the tail of the JSONL log at $CE_LOG_PATH (default "
        "./tmp/cluster.log) as a human-readable transcript; `status` "
        "reports the state of backgrounded runs (list all, or detail by "
        "session_id); `kill` terminates a backgrounded run by session_id. "
        "`run` auto-backgrounds after 10s (default, override via "
        "`CE_AUTO_BG_TIMEOUT_S` for known-long commands) and "
        "hard-kills after `timeout_s` (default 1800s, `CE_TIMEOUT_S`; "
        "set to 0 to disable). Backgrounded runs return `BACKGROUNDED "
        "session=<id>` \u2014 poll with `tail -s <id>`, inspect with "
        "`status`, terminate with `kill`. `status` and `kill` only see "
        "the caller's own backgrounded runs (filtered by the caller's "
        "`_meta.threadId`, falling back to the `mcp-session-id` header); "
        "other clients' runs are not enumerable. If the HTTP client "
        "disconnects mid-`run` (e.g. the turn is interrupted), the SSH "
        "proc is also registered as a backgrounded session \u2014 the "
        "response is gone, so find it with `status` (no args) and "
        "recover with `tail`/`kill`. Full "
        "`run` output is logged to the same JSONL file; the returned "
        "text is the head/tail-truncated merged stream with `[stderr]` "
        "tags on stderr lines. `tail` and `status` polling is rate-limited "
        "per session (max `CE_TAIL_STATUS_MAX_CALLS` per "
        "`CE_TAIL_STATUS_WINDOW_S`s) \u2014 over the limit calls are refused "
        "with a backoff recommendation. `put` and `get` are foreground only (no "
        "auto-backgrounding) and honor the same hard `timeout_s` (default "
        "1800s; set to 0 to disable). All three exec tools accept "
        "`hostname=\"localhost\"` to skip the inner ssh and run directly "
        "on the bastion (e.g. for ansible playbooks via `cd /etc/ansible "
        "&& ./run-playbook.sh ...`), and accept `user` to force a "
        "specific SSH login on the target (e.g. `user=\"admin\"` for "
        "macOS nodes where root login is denied). The server requires "
        "HTTP Bearer authentication on every request; the token lives in "
        "the file logged at startup (default `~/.config/cluster-exec/"
        "bearer`, mode 0600)."
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(_URL_BASE),
        resource_server_url=AnyHttpUrl(_URL_BASE),
    ),
    token_verifier=auth.StaticTokenVerifier(_BEARER_TOKEN),
)


_LOG_PATH = Path(os.environ.get("CE_LOG_PATH", "tmp/cluster.log"))
_BASTION = os.environ.get("CE_BASTION")
_TIMEOUT_S = _env_float("CE_TIMEOUT_S", 1800.0)
# Foreground cap on the `run` tool call — 10s by default, overridable via
# `CE_AUTO_BG_TIMEOUT_S` for deployments where a known-long command
# (ansible deploy, big journalctl dump) should hold the foreground
# rather than auto-backgrounding. The MCP SDK's streamable-HTTP client
# keeps the same `mcp-session-id` across reconnects within one process,
# so a long-running call that survives a network blip can still be polled
# by the same client; setting this higher is mainly useful when the agent
# knows up front that a particular call will exceed 10s and would rather
# wait than deal with the BACKGROUNDED dance.
_AUTO_BG_S = _env_float("CE_AUTO_BG_TIMEOUT_S", 10.0)
_PROGRESS_INTERVAL_S = _env_float("CE_PROGRESS_INTERVAL_S", 1.0)
_PROGRESS_TAIL_LINES = _env_int("CE_PROGRESS_TAIL_LINES", 50)

# Anti busy-polling guard for the `tail`/`status` tools. Applied per
# (tool, session) key: refuse calls more often than `_TAIL_STATUS_MAX_CALLS`
# in `_TAIL_STATUS_WINDOW_S` seconds and tell the agent to sleep at least
# `_TAIL_STATUS_RECOMMENDED_SLEEP_S` before polling again.
_TAIL_STATUS_MAX_CALLS = _env_int("CE_TAIL_STATUS_MAX_CALLS", 3)
_TAIL_STATUS_WINDOW_S = _env_float("CE_TAIL_STATUS_WINDOW_S", 20.0)
_TAIL_STATUS_RECOMMENDED_SLEEP_S = _env_float(
    "CE_TAIL_STATUS_RECOMMENDED_SLEEP_S", 10.0
)
_TAIL_STATUS_LIMITER = ratelimit.RateLimiter(
    max_calls=_TAIL_STATUS_MAX_CALLS,
    window_s=_TAIL_STATUS_WINDOW_S,
    recommended_sleep_s=_TAIL_STATUS_RECOMMENDED_SLEEP_S,
)


def _poll_allowed(
    tool: str, session: str | None, limiter: ratelimit.RateLimiter | None = None
) -> str | None:
    """Return a refusal message if `tool` on `session` is over its limit.

    Refused calls don't consume a slot, so a caller that keeps hitting the
    limit keeps getting a stable backoff recommendation until the window
    clears. `session=None` (tail with no filter, status listing) shares one
    unscoped bucket per tool. Pass `limiter` for tests; defaults to the
    server-wide singleton.
    """
    limiter = limiter or _TAIL_STATUS_LIMITER
    key = f"{tool}:{session or '_unscoped'}"
    if limiter.allow(key):
        return None
    wait = max(
        limiter.recommended_sleep_s,
        math.ceil(limiter.backoff_seconds(key)),
    )
    scope = f"session {session!r}" if session else "the unscoped log/listing"
    return (
        f"RATE-LIMITED: too many {tool} calls for {scope} "
        f"(max {limiter.max_calls} per {int(limiter.window_s)}s). "
        f"Back off \u2014 sleep at least {int(wait)}s before polling again."
    )


# Redaction helpers used by the debug-only request capture middleware
# (`CE_CAPTURE_REQUESTS=<path>` in `main()`). The capture log on disk
# must not leak the bearer token or project-specific details like git
# remote URLs or commit hashes — those go through `request_capture`
# below. Pure functions so they're easy to unit-test without spinning
# up a starlette app.

_SENSITIVE_HEADERS = {"authorization"}


def _redact_authorization(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Replace `Authorization` header values with `Bearer ***`.

    Header lookups are case-insensitive on the wire, but Starlette's
    `request.headers.items()` yields the original-case names, so we
    match case-insensitively here to be safe.
    """
    return [(k, "Bearer ***" if k.lower() in _SENSITIVE_HEADERS else v) for k, v in headers]


def _scrub_git_remote(url: object) -> object:
    """Reduce a git remote URL to just the hostname.

    Accepts `user@host:path`, `https://host/...`, `ssh://user@host/...`,
    etc., and returns the bare hostname so the on-disk capture doesn't
    leak project identity or repo paths. Non-strings pass through.
    """
    if not isinstance(url, str):
        return url
    tail = url.split("@", 1)[-1]  # strip user@
    head = tail.split("://", 1)[-1]  # strip scheme://
    host = head.split(":", 1)[0].split("/", 1)[0]
    return host


def _scrub_body_json(obj: object) -> object:
    """Walk a parsed JSON body and redact known sensitive fields.

    Currently handles codex's `x-codex-turn-metadata.workspaces[*]`:
    reduces `associated_remote_urls` to hostname only and replaces
    `latest_git_commit_hash` with `***`. Unknown shapes pass through.
    """
    if not isinstance(obj, dict):
        return obj
    try:
        turn_md = obj.get("params", {}).get("_meta", {}).get("x-codex-turn-metadata")  # type: ignore[union-attr]
    except AttributeError:
        turn_md = None
    if isinstance(turn_md, dict):
        workspaces = turn_md.get("workspaces")
        if isinstance(workspaces, dict):
            for ws in workspaces.values():
                if not isinstance(ws, dict):
                    continue
                remote = ws.get("associated_remote_urls")
                if isinstance(remote, dict):
                    for k, url in list(remote.items()):
                        remote[k] = _scrub_git_remote(url)
                ws["latest_git_commit_hash"] = "***"
    return obj


def _redact_body_text(text: str) -> str:
    """Try to parse `text` as JSON and redact sensitive fields.

    Non-JSON bodies (e.g. plain GET requests) pass through unchanged.
    Returns the redacted JSON re-serialised in the original form so
    the capture log still reads naturally.
    """
    if not text:
        return text
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    obj = _scrub_body_json(obj)
    return json.dumps(obj, ensure_ascii=False)


def _principal_from_ctx(ctx: Context) -> str | None:
    """Return a stable identifier for the calling client.

    Used as the `principal` on backgrounded sessions so the no-arg `status`
    listing only shows the caller's own runs. By-ID `status`, `tail`, and
    `kill` are not scoped by it (an agent that was handed a `session_id` can
    inspect and terminate another thread's run). Preference order:

      1. `params._meta.threadId` from the JSON-RPC request body. The MCP
         `_meta` object is the conventional slot for protocol-level
         metadata; clients that want a stable session identifier across
         reconnects within one process (e.g. codex, which sends its
         thread ID here on every `tools/call`) populate `threadId` at
         the top of `_meta`. This survives a transport-level reconnect
         where the `mcp-session-id` header would rotate.
      2. `mcp-session-id` header. Transport-level identifier — the same
         client reuses it across reconnects within one process but the
         server's session manager can expire it independently. Falls
         back to this for clients that don't populate `_meta`.

    Returns `None` if neither is present — those sessions are still
    recorded but won't surface in any filtered listing.
    """
    try:
        request_ctx = ctx.request_context
    except (AttributeError, ValueError):
        return None

    # 1. Prefer the MCP `_meta.threadId`. FastMCP exposes the request's
    #    `_meta` object as `request_context.meta`; it's a TypedDict with
    #    extra_items=Any, so non-standard keys round-trip here.
    meta = getattr(request_ctx, "meta", None)
    if meta is not None:
        try:
            thread_id = meta.get("threadId")
        except Exception:
            thread_id = None
        if thread_id:
            return str(thread_id)

    # 2. Fall back to the transport-level `mcp-session-id` header.
    request = getattr(request_ctx, "request", None)
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("mcp-session-id")
    except Exception:
        return None


@SERVER.tool(
    name="run",
    description=(
        "Run a shell command on an internal host via SSH. Two-hop: outer "
        "ssh to $CE_BASTION, inner ssh to the target. Returns the first "
        "`head_lines` and last `tail_lines` of merged stdout/stderr "
        "(defaults 25/25), prefixed with a status line. Full output is "
        "logged to ./tmp/cluster.log as JSONL. "
        "`run` auto-backgrounds after 10s (default, override via "
        "`CE_AUTO_BG_TIMEOUT_S` for known-long commands) and returns "
        "`BACKGROUNDED session=<id>` for polling via `tail -s <id>`. "
        "Hard `timeout_s` (default 1800s, `CE_TIMEOUT_S`) SIGTERMs the "
        "process if it hasn't exited by then. If the HTTP client "
        "disconnects mid-run (e.g. the turn is interrupted), the proc "
        "is also registered as a backgrounded session \u2014 the "
        "response is already gone, so discover it with `status` (no args) "
        "or `tail -s <id>`, terminate it with `kill`. Pass "
        "`hostname=\"localhost\"` to skip the inner ssh and run the "
        "command directly on the bastion (useful for ansible playbooks "
        "via `cd /etc/ansible && ./run-playbook.sh ...`). Pass `user` "
        "to force a specific SSH login on the target (e.g. "
        "`user=\"admin\"` for macOS nodes where root login is denied); "
        "defaults to whatever the bastion's ssh_config says for the host. "
        "Rejected at the top level (with guidance): ssh, head/tail other "
        "than `-1` (use the run() `head_lines`/`tail_lines` args instead), "
        "headtail, and sleep > 10s."
    ),
)
async def run(
    command: str,
    hostname: str,
    head_lines: int = 25,
    tail_lines: int = 25,
    timeout_s: float = _TIMEOUT_S,
    user: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[TextContent]:
    if not command:
        return [TextContent(type="text", text="ERROR: command must not be empty")]
    if not hostname:
        return [TextContent(type="text", text="ERROR: hostname must not be empty")]
    if not _BASTION:
        return [
            TextContent(
                type="text",
                text="ERROR: CE_BASTION is not set. Export it before starting opencode.",
            )
        ]
    if timeout_s < 0:
        return [TextContent(type="text", text=f"ERROR: timeout_s must be non-negative, got {timeout_s}")]
    if timeout_s > 0 and timeout_s <= _AUTO_BG_S:
        return [
            TextContent(
                type="text",
                text=f"ERROR: timeout_s ({timeout_s}) must be greater than the auto-bg threshold ({_AUTO_BG_S}s; CE_AUTO_BG_TIMEOUT_S)",
            )
        ]

    denied = validate.validate_command(command)
    if denied is not None:
        return [TextContent(type="text", text=f"ERROR: {denied}")]

    log.init(_LOG_PATH)
    session_id = secrets.token_hex(4)
    cwd = os.getcwd()
    principal = _principal_from_ctx(ctx)

    cancel = asyncio.Event()

    async def report(progress: int, message: str) -> None:
        await ctx.report_progress(progress, message=message or None)

    async def bridge_cancel() -> None:
        try:
            await ctx.cancel_requested.wait()  # type: ignore[attr-defined]
        except Exception:
            return
        cancel.set()

    bridge_task = asyncio.create_task(bridge_cancel())

    try:
        text = await exec.run(
            bastion=_BASTION,
            target_host=hostname,
            command=command,
            session_id=session_id,
            timeout_s=timeout_s,
            auto_bg_after_s=_AUTO_BG_S,
            progress_interval_s=_PROGRESS_INTERVAL_S,
            progress_tail_lines=_PROGRESS_TAIL_LINES,
            head_lines=head_lines,
            tail_lines=tail_lines,
            cwd=cwd,
            user=user,
            on_progress=report,
            cancel=cancel,
            principal=principal,
        )
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except BaseException:
            pass
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="put",
    description=(
        "Upload a local file to a target host via SSH (binary-safe, "
        "foreground only \u2014 never auto-backgrounds). Streams bytes "
        "through SSH stdin so the payload is never staged on the "
        "bastion. Takes a `local_path` (str), `remote_path` (str), and "
        "`hostname` (str) \u2014 the latter is an internal host name "
        "(e.g. `app1`, `db2`, \u2026). Pass `hostname=\"localhost\"` to "
        "skip the inner ssh and write directly on the bastion. Pass "
        "`user` to force a specific SSH login on the target (e.g. "
        "`user=\"admin\"` for macOS nodes where root login is denied); "
        "defaults to whatever the bastion's ssh_config says for the "
        "host. Honors `timeout_s` (default 1800s, `CE_TIMEOUT_S`; set to "
        "0 to disable) as a hard timeout (SIGTERM, then SIGKILL after "
        "2s). Returns a status line: `OK bytes=N -> <host>:<path> "
        "duration=X.Xs` on success, `FAILED exit=<code> duration=X.Xs` "
        "on non-zero exit, `CANCELLED after X.Xs (timeout)` on hard "
        "timeout, `CANCELLED after X.Xs (cancelled by client)` on "
        "`notifications/cancelled`, or `ERROR: <message>` if the local "
        "file is missing or not a regular file. Full stderr is logged "
        "to ./tmp/cluster.log as JSONL."
    ),
)
async def put(
    local_path: str,
    remote_path: str,
    hostname: str,
    timeout_s: float = _TIMEOUT_S,
    user: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[TextContent]:
    if not local_path:
        return [TextContent(type="text", text="ERROR: local_path must not be empty")]
    if not remote_path:
        return [TextContent(type="text", text="ERROR: remote_path must not be empty")]
    if not hostname:
        return [TextContent(type="text", text="ERROR: hostname must not be empty")]
    if not _BASTION:
        return [
            TextContent(
                type="text",
                text="ERROR: CE_BASTION is not set. Export it before starting opencode.",
            )
        ]
    if timeout_s < 0:
        return [TextContent(type="text", text=f"ERROR: timeout_s must be non-negative, got {timeout_s}")]

    log.init(_LOG_PATH)
    session_id = secrets.token_hex(4)
    cwd = os.getcwd()

    cancel = asyncio.Event()

    async def bridge_cancel() -> None:
        try:
            await ctx.cancel_requested.wait()  # type: ignore[attr-defined]
        except Exception:
            return
        cancel.set()

    bridge_task = asyncio.create_task(bridge_cancel())

    try:
        text = await exec.put_file(
            bastion=_BASTION,
            target_host=hostname,
            local_path=local_path,
            remote_path=remote_path,
            session_id=session_id,
            timeout_s=timeout_s,
            cwd=cwd,
            user=user,
            cancel=cancel,
        )
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except BaseException:
            pass
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="get",
    description=(
        "Download a file from a target host to the local filesystem "
        "via SSH (binary-safe, foreground only \u2014 never auto-"
        "backgrounds). Streams bytes through SSH stdout and writes "
        "them to `local_path` as they arrive. Takes a `remote_path` "
        "(str), `local_path` (str), and `hostname` (str) \u2014 the "
        "latter is an internal host name (e.g. `app1`, `db2`, \u2026). "
        "Pass `hostname=\"localhost\"` to skip the inner ssh and read "
        "directly from the bastion. Pass `user` to force a specific "
        "SSH login on the target (e.g. `user=\"admin\"` for macOS nodes "
        "where root login is denied); defaults to whatever the "
        "bastion's ssh_config says for the host. Honors `timeout_s` "
        "(default 1800s, `CE_TIMEOUT_S`; set to 0 to disable) as a hard timeout "
        "(SIGTERM, then SIGKILL after 2s). Returns a status line: "
        "`OK bytes=N <host>:<path> -> <local> duration=X.Xs` on "
        "success, `FAILED exit=<code> duration=X.Xs` on non-zero exit, "
        "`CANCELLED after X.Xs (timeout)` on hard timeout, "
        "`CANCELLED after X.Xs (cancelled by client)` on "
        "`notifications/cancelled`, or `ERROR: <message>` if the "
        "local parent directory is missing or `local_path` is not a "
        "regular file. Full stderr is logged to ./tmp/cluster.log as "
        "JSONL."
    ),
)
async def get(
    remote_path: str,
    local_path: str,
    hostname: str,
    timeout_s: float = _TIMEOUT_S,
    user: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[TextContent]:
    if not remote_path:
        return [TextContent(type="text", text="ERROR: remote_path must not be empty")]
    if not local_path:
        return [TextContent(type="text", text="ERROR: local_path must not be empty")]
    if not hostname:
        return [TextContent(type="text", text="ERROR: hostname must not be empty")]
    if not _BASTION:
        return [
            TextContent(
                type="text",
                text="ERROR: CE_BASTION is not set. Export it before starting opencode.",
            )
        ]
    if timeout_s < 0:
        return [TextContent(type="text", text=f"ERROR: timeout_s must be non-negative, got {timeout_s}")]

    log.init(_LOG_PATH)
    session_id = secrets.token_hex(4)
    cwd = os.getcwd()

    cancel = asyncio.Event()

    async def bridge_cancel() -> None:
        try:
            await ctx.cancel_requested.wait()  # type: ignore[attr-defined]
        except Exception:
            return
        cancel.set()

    bridge_task = asyncio.create_task(bridge_cancel())

    try:
        text = await exec.get_file(
            bastion=_BASTION,
            target_host=hostname,
            remote_path=remote_path,
            local_path=local_path,
            session_id=session_id,
            timeout_s=timeout_s,
            cwd=cwd,
            user=user,
            cancel=cancel,
        )
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except BaseException:
            pass
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="tail",
    description=(
        "Tail the structured JSONL log at $CE_LOG_PATH (default "
        "./tmp/cluster.log) and render the last `tail_lines` events "
        "(default 50) as a human-readable transcript: timestamps, "
        "session IDs, event types (start/line/end), and event bodies. "
        "Optional `session` filter limits output to one session. The "
        "raw JSONL is written by `run` \u2014 useful for inspecting a "
        "backgrounded run (the `session` it returned) or recovering "
        "output that was truncated in a `run` result. "
        "Polling is rate-limited per session (at most "
        "`CE_TAIL_STATUS_MAX_CALLS` calls per `CE_TAIL_STATUS_WINDOW_S` "
        "seconds); over the limit the call is refused with a backoff "
        "recommendation."
    ),
)
async def tail(
    tail_lines: int = 50,
    session: str | None = None,
) -> list[TextContent]:
    if tail_lines < 0:
        return [
            TextContent(
                type="text",
                text=f"ERROR: tail_lines must be non-negative, got {tail_lines}",
            )
        ]
    refused = _poll_allowed("tail", session)
    if refused is not None:
        return [TextContent(type="text", text=refused)]
    log.init(_LOG_PATH)
    events = log.tail(_LOG_PATH, n=tail_lines, session=session)
    text = log.render(events, path=_LOG_PATH, requested=tail_lines)
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="kill",
    description=(
        "Terminate a backgrounded run by its `session_id` (the value "
        "returned by the `run` tool when it auto-backgrounded). Not scoped "
        "to the caller's thread \u2014 a `session_id` handed to you by another "
        "agent can be killed here. Sends SIGTERM, escalates to SIGKILL after "
        "2s. Returns how many lines of output were captured before the kill "
        "\u2014 fetch the full transcript with `tail -s <session_id>`."
    ),
)
async def kill(
    session_id: str,
) -> list[TextContent]:
    if not session_id:
        return [TextContent(type="text", text="ERROR: session_id must not be empty")]
    log.init(_LOG_PATH)
    bg = exec.get_backgrounded(session_id)
    if bg is None:
        return [
            TextContent(
                type="text",
                text=f"ERROR: session {session_id!r} not found (already finished, killed, or never existed)",
            )
        ]
    started = bg.started
    killed = exec.kill_backgrounded(session_id)
    if not killed:
        return [TextContent(type="text", text=f"ERROR: failed to kill session {session_id!r}")]
    line_count = sum(
        1 for _ in log.tail(_LOG_PATH, n=10_000_000, session=session_id) if _.get("event") == "line"
    )
    duration = time.monotonic() - started
    return [
        TextContent(
            type="text",
            text=f"KILLED session={session_id} after {duration:.1f}s (captured {line_count} lines) \u2014 view with tail -s {session_id}",
        )
    ]


def _session_state(bg, log_path: Path) -> str:
    elapsed = time.monotonic() - bg.started
    rc = bg.proc.returncode
    line_count = sum(
        1 for _ in log.tail(log_path, n=10_000_000, session=bg.session_id) if _.get("event") == "line"
    )
    if rc is None:
        remaining = max(0.0, bg.timeout_s - elapsed)
        state = f"running elapsed={elapsed:.1f}s lines={line_count} hard_timeout_in={remaining:.1f}s"
    else:
        state = f"exited exit={rc} elapsed={elapsed:.1f}s lines={line_count}"
    if bg.orphaned:
        state = "orphaned-by-disconnect " + state
    return state


@SERVER.tool(
    name="status",
    description=(
        "Report the state of backgrounded runs. With no argument, lists the "
        "active sessions owned by the caller's thread (filtered by "
        "`_meta.threadId`, falling back to `mcp-session-id`). With a "
        "`session_id` it reports any run on this server \u2014 including one "
        "started by another thread/agent that handed you the id \u2014 showing "
        "whether the process is still running or has exited, line count so "
        "far, and remaining time before the hard timeout kills it. "
        "Polling is rate-limited per session (at most "
        "`CE_TAIL_STATUS_MAX_CALLS` calls per `CE_TAIL_STATUS_WINDOW_S` "
        "seconds); over the limit the call is refused with a backoff "
        "recommendation."
    ),
)
async def status(
    session_id: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> list[TextContent]:
    log.init(_LOG_PATH)
    if session_id is not None and not session_id:
        return [TextContent(type="text", text="ERROR: session_id must not be empty when provided")]
    refused = _poll_allowed("status", session_id)
    if refused is not None:
        return [TextContent(type="text", text=refused)]
    if session_id:
        bg = exec.get_backgrounded(session_id)
        if bg is None:
            return [
                TextContent(
                    type="text",
                    text=f"ERROR: session {session_id!r} not found (already finished, killed, or never existed)",
                )
            ]
        text = (
            f"session={bg.session_id} host={bg.target_host} cmd={bg.command!r} "
            f"timeout_s={bg.timeout_s} {_session_state(bg, _LOG_PATH)}"
        )
    else:
        sessions = exec.list_backgrounded(principal=_principal_from_ctx(ctx))
        if not sessions:
            text = "no backgrounded sessions"
        else:
            lines = [f"{len(sessions)} backgrounded session(s):"]
            for sid in sessions:
                bg_item = exec.get_backgrounded(sid)
                if bg_item is None:
                    continue
                lines.append(
                    f"  {sid} host={bg_item.target_host} cmd={bg_item.command!r} "
                    f"{_session_state(bg_item, _LOG_PATH)}"
                )
            text = "\n".join(lines)
    return [TextContent(type="text", text=text)]


def main() -> None:
    host = os.environ.get("CE_HTTP_HOST", "127.0.0.1")
    port = _env_int("CE_HTTP_PORT", 4003)
    path = os.environ.get("CE_HTTP_PATH", "/mcp")
    print(f"bearer token file: {_BEARER_PATH} (mode 0600)", flush=True)
    print(f"listening: http://{host}:{port}{path}", flush=True)

    capture_path = os.environ.get("CE_CAPTURE_REQUESTS")
    if capture_path:
        # Debug-only: log every incoming HTTP request (method, path,
        # headers, body) to a JSONL file. Used to observe what clients
        # actually send on the wire — set CE_CAPTURE_REQUESTS=<path>
        # (e.g. `./tmp/cluster-exec-requests.jsonl`) before --start.
        # Sensitive fields are redacted before write: bearer tokens
        # become `Bearer ***`, and codex `x-codex-turn-metadata.workspaces`
        # has git remote URLs reduced to hostname and commit hashes
        # replaced with `***`.
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware

        class _RequestCapture(BaseHTTPMiddleware):
            def __init__(self, app, path: str) -> None:
                super().__init__(app)
                self._path = path

            async def dispatch(self, request: Request, call_next):
                body_bytes = await request.body()
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": _redact_authorization(list(request.headers.items())),
                    "body": _redact_body_text(body_bytes.decode("utf-8", errors="replace")),
                }
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                return await call_next(request)

        starlette_app = SERVER.streamable_http_app(
            streamable_http_path=path,
            host=host,
        )
        starlette_app.add_middleware(_RequestCapture, path=capture_path)
        print(f"capturing requests to {capture_path}", flush=True)
        uvicorn.run(starlette_app, host=host, port=port, log_level="warning")
        return

    SERVER.run(transport="streamable-http", host=host, port=port, streamable_http_path=path)


# Admin endpoint used by `cedaemon --stop` to ask the server to terminate
# itself without relying on the pid file (which is unreliable across PID
# namespaces). FastMCP's custom_route does not require authorization, so we
# enforce bearer auth manually. The handler kills any backgrounded SSH
# sessions, schedules a SIGTERM to self (uvicorn catches it and shuts down
# gracefully — no in-flight requests are dropped), and returns 200.
@SERVER.custom_route("/stop", methods=["POST"])
async def _stop(request: Request) -> JSONResponse:
    auth_header = request.headers.get("authorization", "")
    token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""
    if not token or not secrets.compare_digest(token, _BEARER_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    killed = exec.kill_all_backgrounded()
    # Defer SIGTERM until after the response is flushed.
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return JSONResponse({"stopping": True, "killed_backgrounded": killed})


_lock_down_tool_schemas()


if __name__ == "__main__":
    main()
