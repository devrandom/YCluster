# cluster-exec

Local MCP server for running shell commands on internal hosts through an SSH bastion.

## What it does

Exposes two tools:

- **`run`** — run a command on an internal host:
  1. Takes a `command` (str) and a `hostname` (str) — the latter is an internal host name (e.g. `app1`, `db2`, …). Whatever the bastion can reach via its `ssh_config`.
  2. Runs the command via a two-hop SSH pattern:
     ```
     ssh -t "$CE_BASTION" 'ssh <hostname> "<command>"'
     ```
     The outer ssh uses `-t` so SIGTERM from cancellation propagates to the inner command.
  3. Captures stdout and stderr as separate streams (chronologically merged in the result), with stderr lines tagged `[stderr]`.
  4. Truncates the result to the first `head_lines` and last `tail_lines` of merged output (defaults 25/25), inserting a `… [N lines elided] …` marker between them.
  5. Returns the result as MCP text content, prefixed with a status line:
     - `OK exit=0 duration=4.2s`
     - `FAILED exit=2 duration=4.2s`
     - `CANCELLED after 12.3s (partial output below)`
     - `ERROR: <message>` (could not start)

- **`cetail`** — render the tail of the JSONL log (see [Logging](#logging)) as a human-readable transcript. Takes `tail_lines` (default 50) and an optional `session` filter. Useful for inspecting what just ran or recovering output that was truncated in a `run` result.

## Security warning

This tool grants the LLM **whatever SSH access your local `ssh` config has to `$CE_BASTION` and from there to `<hostname>`**. There are no allowlists, denylists, or confirmation steps. The privilege is intentional — the human running opencode is the admin.

## Logging

Every call writes its full stdout + stderr (untruncated) to `./tmp/cluster.log` as JSONL events:

```json
{"ts":"…","event":"start","session":"a1b2c3d4","host":"app1","cmd":"uptime","cwd":"…"}
{"ts":"…","event":"line","session":"a1b2c3d4","stream":"stdout","line":" 14:32:01 up 12 days, …"}
{"ts":"…","event":"end","session":"a1b2c3d4","exit":0,"duration_s":4.2,"status":"ok","lines":3}
```

Multiple concurrent sessions are safe — a single global lock ensures lines aren't torn. No auto-cleanup; the human deletes `./tmp/cluster-*.log` when they want.

Useful queries:

```bash
tail -f ./tmp/cluster.log | jq -c 'select(.session=="a1b2c3d4")'
jq -c 'select(.event=="end" and .status=="cancelled")' ./tmp/cluster.log
jq -r 'select(.event=="start") | "\(.ts) \(.host) \(.cmd)"' ./tmp/cluster.log
```

Or call the `cetail` MCP tool — same idea, but the events are pre-rendered as a human-readable transcript (one line per event with timestamp, session, type, and body) instead of raw JSON. Output looks like:

```
[cetail] 4 events from tmp/cluster.log (tail=50)

2026-07-11T12:54:23.123Z  [a1b2c3d4]  start   host=s4 cmd='journalctl -u etcd' cwd=/...
2026-07-11T12:54:23.456Z  [a1b2c3d4]  line    ● s1: etcd running
2026-07-11T12:54:23.789Z  [a1b2c3d4]  line    [stderr] warning: deprecation
2026-07-11T12:54:24.012Z  [a1b2c3d4]  end     exit=0 duration=1.234s status=ok lines=3
```

For humans at the terminal, the same renderer is wrapped as a CLI script:

```bash
bash mcp/cluster-exec/cetail                  # last 50 events
bash mcp/cluster-exec/cetail -n 100           # last 100
bash mcp/cluster-exec/cetail -s a1b2c3d4      # one session only
bash mcp/cluster-exec/cetail -f               # follow new events (Ctrl-C to stop)
CE_LOG_PATH=/path/to/x.log bash mcp/cluster-exec/cetail
```

`-f` waits for the file if it doesn't exist yet, prints the initial tail, then polls (default 0.25s, override with `--interval`) and prints each new event as it arrives. Combines with `-s` to watch one session.

## Cancellation & timeouts

- **Server-side timeout**: enforced at `CE_TIMEOUT_S` (default 60s). If the command runs longer, SIGTERM is sent, then SIGKILL after 2s grace.
- **Client cancellation**: registered via the MCP cancellation hook when the client sends `notifications/cancelled`. The Python MCP SDK does not always emit these reliably (see modelcontextprotocol/python-sdk#2507), so the timeout is the primary kill mechanism.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `CE_BASTION` | (required) | Entry host the outer ssh hops through. Should resolve in your local `~/.ssh/config`. |
| `CE_LOG_PATH` | `tmp/cluster.log` | JSONL log path (relative to MCP server CWD). |
| `CE_TIMEOUT_S` | `60` | Per-call hard timeout in seconds. |
| `CE_PROGRESS_INTERVAL_S` | `1.0` | Progress notification cadence in seconds. |
| `CE_PROGRESS_TAIL_LINES` | `50` | Lines of cumulative tail shown in each progress notification. |

## Development

Run the tests:

```bash
cd mcp/cluster-exec
uv run --project . python -m unittest discover tests -v
```

Smoke-test the server (then trigger `run` from opencode):

```bash
uv run --project mcp/cluster-exec python -m cluster_exec
```
