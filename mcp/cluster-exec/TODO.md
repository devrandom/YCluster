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

## persist _BACKGROUNDED across daemon restarts — done

The registry is snapshotted to `$CE_STATE_PATH` (default
`tmp/cluster-exec.state.json`, mode 0600) on every mutation and restored by
the MCP lifespan. Restored sessions have no live `asyncio.Process`; they are
tagged `orphaned-by-restart`, polled with local `pgrep -f CE_SESSION=<id>`,
and killed by scanning `/proc` for that marker, then SIGTERM/SIGKILL. The
ssh argv carries the marker through `-o SetEnv=CE_SESSION=<id>`, so no
remote round-trip is needed.

Finished and lost sessions stay in the registry so `status` retains exit
state across restarts. The state file is not reaped automatically yet; it is
small, and the operator can delete it when it becomes noisy.
