# TODOs

Open work for cluster-exec. Move items here when deferring — keeps the
codebase free of in-source `# TODO` clutter while preserving the
rationale.

## client-isolation for backgrounded sessions — done

`_BACKGROUNDED` (`src/cluster_exec/exec.py`) is filtered by a `principal`
field populated by `_principal_from_ctx`. Preference order:

  1. `params._meta.threadId` (the MCP convention for a stable per-process
     session ID — codex puts its thread ID here on every `tools/call`).
     Survives transport reconnects within one client process.
  2. `mcp-session-id` request header (transport-level — same value across
     reconnects within one process, but the server's session manager
     can expire it independently). Fallback for clients that don't
     populate `_meta`.

`status` and `kill` only see the caller's own runs — attempts on other
clients' sessions return `ERROR: session not found` so they cannot be
enumerated. The `/stop` admin endpoint and `kill_all_backgrounded` still
bypass the filter for daemon shutdown.

The bearer token alone authenticates the connection (anyone with the
token can connect), but per-client visibility is scoped by the caller's
stable identifier — one client cannot `status` or `kill` another's runs.
The JSONL log (`./tmp/cluster.log`) remains the source of truth for
everything (read-only via `tail -s <id>`).

Trade-off: orphaned sessions from a disconnected client are tagged with
that client's principal and become invisible to subsequent clients.
Two flavours of orphan, depending on which signal was used:

  - codex (`_meta.threadId`): fresh per `codex exec` invocation, so a
    resumed codex session gets a new ID and cannot see prior orphans.
    Still readable from the JSONL log via `tail -s <id>`, cleaned up by
    the hard `timeout_s` watcher (default 1800s) or `cedaemon --stop`
    (unfiltered admin path).
  - opencode / other (`mcp-session-id`): the streamable-HTTP client
    keeps the same ID across reconnects within one process, so an
    orphan can be picked up by the same client after a network blip.
    A fresh process invocation gets a new ID and cannot see prior
    orphans — same recovery path as above.

## persist _BACKGROUNDED across daemon restarts

`_BACKGROUNDED` lives only in process memory. When `cedaemon` is
restarted (operator action, host reboot, codex bwrap death), every
in-flight backgrounded SSH proc becomes unreachable from the new
daemon — `status` returns "not found" for them even though the proc
may still be running, and `kill` cannot reach them because the new
daemon has no `asyncio.subprocess.Process` handle. The JSONL log
(`tmp/cluster.log`) preserves the start/line/end events, but reading
the log doesn't give us the SSH PID we'd need to actually terminate
the orphan.

Key insight: the orphaned SSH proc is a **local** child of the
daemon (launched via `asyncio.create_subprocess_exec("ssh", bastion,
...)` — `exec.py`). On a daemon crash without bwrap teardown it gets
reparented to init and keeps running locally. Killing it closes the
SSH channel → the bastion-side inner ssh and the remote command die
via SIGHUP. **No remote round-trip is needed** — the termination path
is entirely local: `pkill -f CE_SESSION=<id>`. The prior sketch's
`ssh <host> "pkill ..."` was the wrong target (remote, misses the
local proc, and is unneeded).

When bwrap tears down (codex crash), the local PID namespace is
destroyed and all local ssh children die with it — so the first
local `pgrep` poll after restart finds nothing and the entry is
dropped immediately. No stale `running` state.

Goal: after a restart, `status` should show recovered sessions as
`orphaned-by-restart` so the user knows they may still be running,
`kill` should terminate them via local `pkill`, and the hard
`timeout_s` should be re-armed via a local `pgrep` poll loop (not
the dead `asyncio.Task` — that's lost with the old event loop).

Principal isolation is preserved automatically: recovered orphans
carry the `threadId` of the client that started them, and a codex
resume sends the same `threadId`, so the existing `_visible(principal)`
filter (`exec.py`) matches them correctly. No policy change, no
admin-only visibility hack, no filter bypass. (opencode's
`mcp-session-id` changes on process restart — same orphan-visibility
trade-off already documented above for disconnect orphans, not a new
problem.)

Sketch:

  - Write `_BACKGROUNDED` to `$CE_STATE_PATH` (default
    `tmp/cluster-exec.state.json`) on every mutation. The JSON
    snapshot has everything we need except the live `proc` handle
    and `readers` / `timeout_watcher` tasks — drop those on write,
    restore as `None` on read.
  - Inject `CE_SESSION=<id>` into the local `ssh` argv at command
    construction time (`exec.py`, the `create_subprocess_exec("ssh",
    bastion, ...)` call) so `pgrep`/`pkill -f` can match locally.
    This is a hot-path change (the `run` tool's command construction),
    not just the persistence layer — needs care with the two-hop
    `shlex.quote` path, but the marker goes on the local ssh argv
    (not the remote command), so it doesn't touch the user's command
    string.
  - On startup, load the snapshot and re-register every entry with
    `orphaned=True`. `status` renders them with an
    `orphaned-by-restart` prefix so the caller knows `kill` goes
    through local `pkill`, not the in-process handle.
  - On `kill` for an `orphaned-by-restart` session: `pkill -f
    CE_SESSION=<id>` locally. Best-effort — return success/failure
    based on the local exit code, log it to the JSONL so `tail`
    shows the recovery attempt.
  - Re-arm the hard timeout: a local `pgrep -f CE_SESSION=<id>` poll
    loop (cheap local syscalls, not expensive remote polls). When
    the proc disappears (exited naturally or killed), drop the entry
    from `_BACKGROUNDED` and the snapshot. When `timeout_s` elapses,
    local `pkill`. We don't need a reader task for orphans — the
    proc has no parent pipe in this daemon, so no lines to consume.
  - File rotation: same `tmp/` hygiene as `cluster.log` — no
    auto-cleanup, the operator deletes it. Keep the schema
    versioned (`{"v": 1, "sessions": [...]}`) so future format
    changes don't crash an old daemon reading a new snapshot.

Smaller alternative (option B): skip the snapshot, just walk
`cluster.log` on startup and surface `backgrounded` sessions with
no matching `end` event. Same visibility benefit, but `kill` stays
a no-op for those sessions — the user has to clean up by hand. With
local pkill making `kill`-on-orphan cheap and correct, the snapshot
(option A) is the better choice; the log-walk is only worth it if
we want zero-risk visibility as a first step and can defer `kill`.
