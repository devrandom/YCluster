import asyncio
import os
import secrets
import signal
import time
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import TextContent
from pydantic import AnyHttpUrl, ConfigDict, create_model
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import auth, exec, log


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
        "`run` auto-backgrounds after `auto_bg_after_s` (default 30s; "
        "set to 0 to disable) and hard-kills after `timeout_s` (default "
        "1800s). Backgrounded runs return `BACKGROUNDED session=<id>` "
        "\u2014 poll with `tail -s <id>`, inspect with `status`, "
        "terminate with `kill`. If the HTTP client disconnects mid-`run` "
        "(e.g. the turn is interrupted), the SSH proc is also registered "
        "as a backgrounded session \u2014 the response is gone, so find it "
        "with `status` (no args) and recover with `tail`/`kill`. Full `run` output is logged to the same "
        "JSONL file; the returned text is the head/tail-truncated merged "
        "stream with `[stderr]` tags on stderr lines. `put` and `get` "
        "are foreground only (no auto-backgrounding) and honor the same "
        "hard `timeout_s` (default 1800s; set to 0 to disable). All three "
        "exec tools accept `hostname=\"localhost\"` to skip the inner "
        "ssh and run directly on the bastion (e.g. for ansible "
        "playbooks via `cd /etc/ansible && ./run-playbook.sh ...`), and "
        "accept `user` to force a specific SSH login on the target "
        "(e.g. `user=\"admin\"` for macOS nodes where root login is "
        "denied). The server requires HTTP Bearer authentication on "
        "every request; the token lives in the file logged at startup "
        "(default `~/.config/cluster-exec/bearer`, mode 0600)."
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
_AUTO_BG_S = _env_float("CE_AUTO_BG_TIMEOUT_S", 30.0)
_PROGRESS_INTERVAL_S = _env_float("CE_PROGRESS_INTERVAL_S", 1.0)
_PROGRESS_TAIL_LINES = _env_int("CE_PROGRESS_TAIL_LINES", 50)


@SERVER.tool(
    name="run",
    description=(
        "Run a shell command on an internal host via SSH. Two-hop: outer "
        "ssh to $CE_BASTION, inner ssh to the target. Returns the first "
        "`head_lines` and last `tail_lines` of merged stdout/stderr "
        "(defaults 25/25), prefixed with a status line. Full output is "
        "logged to ./tmp/cluster.log as JSONL. "
        "Two-tier timeouts: soft `auto_bg_after_s` (default 30s, set to 0 "
        "to disable) transitions the run to the background and returns "
        "`BACKGROUNDED session=<id>` for polling via `tail -s <id>`; "
        "hard `timeout_s` (default 1800s) SIGTERMs the process. Both "
        "overridable per call \u2014 set `timeout_s` greater than "
        "`auto_bg_after_s` (or both to 0). If the HTTP client disconnects "
        "mid-run (e.g. the turn is interrupted), the proc is registered as "
        "a backgrounded session \u2014 the response is already gone, so "
        "discover it with `status` (no args) or `tail -s <id>`, terminate "
        "it with `kill`. Pass `hostname=\"localhost\"` "
        "to skip the inner ssh and run the command directly on the "
        "bastion (useful for ansible playbooks via "
        "`cd /etc/ansible && ./run-playbook.sh ...`). Pass `user` to "
        "force a specific SSH login on the target (e.g. `user=\"admin\"` "
        "for macOS nodes where root login is denied); defaults to "
        "whatever the bastion's ssh_config says for the host."
    ),
)
async def run(
    command: str,
    hostname: str,
    head_lines: int = 25,
    tail_lines: int = 25,
    auto_bg_after_s: float = _AUTO_BG_S,
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
    if auto_bg_after_s < 0:
        return [TextContent(type="text", text=f"ERROR: auto_bg_after_s must be non-negative, got {auto_bg_after_s}")]
    if timeout_s < 0:
        return [TextContent(type="text", text=f"ERROR: timeout_s must be non-negative, got {timeout_s}")]
    if auto_bg_after_s > 0 and timeout_s > 0 and timeout_s <= auto_bg_after_s:
        return [
            TextContent(
                type="text",
                text=f"ERROR: timeout_s ({timeout_s}) must be greater than auto_bg_after_s ({auto_bg_after_s})",
            )
        ]

    log.init(_LOG_PATH)
    session_id = secrets.token_hex(4)
    cwd = os.getcwd()

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
            auto_bg_after_s=auto_bg_after_s,
            progress_interval_s=_PROGRESS_INTERVAL_S,
            progress_tail_lines=_PROGRESS_TAIL_LINES,
            head_lines=head_lines,
            tail_lines=tail_lines,
            cwd=cwd,
            user=user,
            on_progress=report,
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
        "output that was truncated in a `run` result."
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
    log.init(_LOG_PATH)
    events = log.tail(_LOG_PATH, n=tail_lines, session=session)
    text = log.render(events, path=_LOG_PATH, requested=tail_lines)
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="kill",
    description=(
        "Terminate a backgrounded run by its `session_id` (the value "
        "returned by the `run` tool when it auto-backgrounded). Sends "
        "SIGTERM, escalates to SIGKILL after 2s. Returns how many lines "
        "of output were captured before the kill \u2014 fetch the full "
        "transcript with `tail -s <session_id>`."
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
        "Report the state of backgrounded runs. With no argument, lists all "
        "active sessions (session_id, target_host, command, elapsed, lines, "
        "hard-timeout countdown). With a `session_id`, shows detailed state "
        "for that session including whether the process is still running "
        "or has exited, line count so far, and remaining time before the "
        "hard timeout kills it."
    ),
)
async def status(
    session_id: str | None = None,
) -> list[TextContent]:
    log.init(_LOG_PATH)
    if session_id is not None and not session_id:
        return [TextContent(type="text", text="ERROR: session_id must not be empty when provided")]
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
        sessions = exec.list_backgrounded()
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
