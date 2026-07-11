import os
import secrets
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.types import TextContent

from . import exec, log

SERVER = MCPServer(
    "cluster-exec",
    instructions=(
        "Two tools: `run` executes shell commands on internal hosts via "
        "SSH through a bastion (grants the configured SSH user full "
        "remote code execution — use sparingly); `cetail` renders the "
        "tail of the JSONL log at $CE_LOG_PATH (default "
        "./tmp/cluster.log) as a human-readable transcript. Full "
        "`run` output is logged to the same JSONL file; the "
        "returned text is the head/tail-truncated merged stream with "
        "[stderr] tags on stderr lines."
    ),
)


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


_LOG_PATH = Path(os.environ.get("CE_LOG_PATH", "tmp/cluster.log"))
_BASTION = os.environ.get("CE_BASTION")
_TIMEOUT_S = _env_float("CE_TIMEOUT_S", 60.0)
_PROGRESS_INTERVAL_S = _env_float("CE_PROGRESS_INTERVAL_S", 1.0)
_PROGRESS_TAIL_LINES = _env_int("CE_PROGRESS_TAIL_LINES", 50)


@SERVER.tool(
    name="run",
    description=(
        "Run a shell command on an internal host via SSH. Two-hop: outer "
        "ssh to $CE_BASTION, inner ssh to the target. Returns the first "
        "`head_lines` and last `tail_lines` of merged stdout/stderr "
        "(defaults 25/25), prefixed with a status line. Full output is "
        "logged to ./tmp/cluster.log as JSONL."
    ),
)
async def run(
    command: str,
    hostname: str,
    head_lines: int = 25,
    tail_lines: int = 25,
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

    log.init(_LOG_PATH)
    session_id = secrets.token_hex(4)
    cwd = os.getcwd()

    async def report(progress: int, message: str) -> None:
        await ctx.report_progress(progress, message=message or None)

    text = await exec.run(
        bastion=_BASTION,
        target_host=hostname,
        command=command,
        session_id=session_id,
        timeout_s=_TIMEOUT_S,
        progress_interval_s=_PROGRESS_INTERVAL_S,
        progress_tail_lines=_PROGRESS_TAIL_LINES,
        head_lines=head_lines,
        tail_lines=tail_lines,
        cwd=cwd,
        on_progress=report,
    )
    return [TextContent(type="text", text=text)]


@SERVER.tool(
    name="cetail",
    description=(
        "Tail the structured JSONL log at $CE_LOG_PATH (default "
        "./tmp/cluster.log) and render the last `tail_lines` events "
        "(default 50) as a human-readable transcript: timestamps, "
        "session IDs, event types (start/line/end), and event bodies. "
        "Optional `session` filter limits output to one session. The "
        "raw JSONL is written by `run` — useful for inspecting what "
        "just ran or recovering output that was truncated in the tool "
        "result."
    ),
)
async def cetail(
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


def main() -> None:
    SERVER.run(transport="stdio")


if __name__ == "__main__":
    main()
