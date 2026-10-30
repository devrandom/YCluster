# cluster-exec

Local MCP server for running shell commands on internal hosts through an SSH bastion.

## What it does

Exposes six tools:

- **`run`** — run a command on an internal host:
  1. Takes a `command` (str) and a `hostname` (str) — the latter is an internal host name (e.g. `app1`, `db2`, …). Whatever the bastion can reach via its `ssh_config`. Pass `hostname="localhost"` to skip the inner ssh and run the command directly on the bastion itself (useful for ansible playbooks — see [Ansible](#ansible)).
  2. Optionally takes a `user` (str) — overrides the SSH login on the inner hop (e.g. `user="admin"` for macOS nodes where root login is denied). Defaults to whatever the bastion's `ssh_config` says for the host. No-op when `hostname="localhost"`.
  3. Runs the command via a two-hop SSH pattern:
     ```
     ssh "$CE_BASTION" 'ssh [<user>@]<hostname> "<command>"'
     ```
     For `hostname="localhost"` this collapses to a single hop:
     ```
     ssh "$CE_BASTION" "<command>"
     ```
  4. Captures stdout and stderr as separate streams (chronologically merged in the result), with stderr lines tagged `[stderr]`.
  5. Truncates the result to the first `head_lines` and last `tail_lines` of merged output (defaults 25/25), inserting a `… [N lines elided] …` marker between them.
  6. Returns the result as MCP text content, prefixed with a status line:
     - `OK exit=0 duration=4.2s`
     - `FAILED exit=2 duration=4.2s`
     - `CANCELLED after 12.3s (timeout)` — hard timeout fired
     - `CANCELLED after 12.3s (cancelled by client)` — `notifications/cancelled` from the client
     - `BACKGROUNDED session=<id> after 10.0s` — soft auto-bg timeout fired; poll with `tail -s <id>`
     - `KILLED session=<id>` — returned by the `kill` tool
     - `ERROR: <message>` (could not start)

  **Two-tier timeouts**:
  - `auto_bg_after_s` is fixed at **10s** server-side — there is no per-call argument. Override globally via `CE_AUTO_BG_TIMEOUT_S` (e.g. `CE_AUTO_BG_TIMEOUT_S=120 bash mcp/cluster-exec/cedaemon --start`) for deployments where a known-long command should hold the foreground rather than auto-backgrounding. Anything still running at the threshold is handed off as `BACKGROUNDED session=<id>` and the tool call returns — known-long commands are expected to be polled with `status` + `tail -s <id>` / `kill` rather than held in the foreground.
  - `timeout_s` (default 1800s, `CE_TIMEOUT_S`) — hard timeout on the backgrounded process itself. SIGTERM, then SIGKILL after 2s. Set to 0 to disable. Must be greater than the auto-bg threshold when enabled.

- **`put`** — upload a local file to an internal host. Takes `local_path` (str), `remote_path` (str), and `hostname` (str) — the latter is an internal host name (e.g. `app1`, `db2`, …). Pass `hostname="localhost"` to skip the inner ssh and write directly on the bastion. Pass `user` (str) to override the SSH login on the inner hop (e.g. `user="admin"` for macOS nodes); defaults to the bastion's `ssh_config`. Streams bytes through SSH stdin (`cat > file` on the remote end) so the payload is binary-safe and never staged on the bastion. Foreground only — never auto-backgrounds. Honors `timeout_s` (default 1800s, `CE_TIMEOUT_S`; set to 0 to disable) as a hard timeout. Returns a status line:
  - `OK bytes=N -> <host>:<path> duration=X.Xs` on success
  - `FAILED exit=<code> duration=X.Xs` on non-zero exit
  - `CANCELLED after X.Xs (timeout)` on hard timeout
  - `CANCELLED after X.Xs (cancelled by client)` on `notifications/cancelled`
  - `ERROR: <message>` if `local_path` is missing or not a regular file

- **`get`** — download a file from an internal host to the local filesystem. Takes `remote_path` (str), `local_path` (str), and `hostname` (str) — the latter is an internal host name. Pass `hostname="localhost"` to skip the inner ssh and read directly from the bastion. Pass `user` (str) to override the SSH login on the inner hop (e.g. `user="admin"` for macOS nodes); defaults to the bastion's `ssh_config`. Streams bytes through SSH stdout (`cat <path>` on the remote end) and writes them to `local_path` as they arrive — binary-safe, never staged on the bastion. Foreground only — never auto-backgrounds. Honors `timeout_s` (default 1800s, `CE_TIMEOUT_S`; set to 0 to disable) as a hard timeout. Returns a status line:
  - `OK bytes=N <host>:<path> -> <local> duration=X.Xs` on success
  - `FAILED exit=<code> duration=X.Xs` on non-zero exit
  - `CANCELLED after X.Xs (timeout)` on hard timeout
  - `CANCELLED after X.Xs (cancelled by client)` on `notifications/cancelled`
  - `ERROR: <message>` if `local_path`'s parent directory is missing or `local_path` is not a regular file

  Both `put` and `get` log their full stderr to `./tmp/cluster.log` as JSONL (queryable with `tail -s <session>`).

- **`tail`** — render the tail of the JSONL log (see [Logging](#logging)) as a human-readable transcript. Takes `tail_lines` (default 50) and an optional `session` filter. Useful for inspecting what just ran, recovering output that was truncated in a `run` result, or watching a backgrounded session.

- **`status`** — list or inspect backgrounded runs **owned by the caller's MCP session** (filtered by the caller's `_meta.threadId`, falling back to the `mcp-session-id` header). With no argument, lists all sessions (session_id, target_host, command, elapsed, line count, hard-timeout countdown), including completed runs that haven't been removed. With a `session_id`, shows detailed state for that session including whether the process is still running or has exited. Other clients' sessions are not enumerable — the bearer token alone authenticates the connection, but per-client visibility is scoped by the caller's stable identifier.

- **`kill`** — terminate a backgrounded run by `session_id`. Only kills sessions owned by the caller's MCP session; attempts on other clients' sessions return `ERROR: session not found`. SIGTERM, then SIGKILL after 2s. Returns how many lines of output were captured before the kill.

`tail` and `status` are rate-limited per session: at most 3 calls per 20 seconds (all tunable via env). A call over the limit is refused with a `RATE-LIMITED: …` message recommending the agent sleep (at least 10s) before polling again, so the agent doesn't busy-poll a long-running session.

## Transport

Streamable HTTP at `http://127.0.0.1:4003/mcp` (configurable via `CE_HTTP_HOST` / `CE_HTTP_PORT` / `CE_HTTP_PATH`). The server runs as a long-lived daemon managed by `bash mcp/cluster-exec/cedaemon`:

```bash
bash mcp/cluster-exec/cedaemon --start    # idempotent; waits until port is bound
bash mcp/cluster-exec/cedaemon --stop     # POST /stop with bearer; the daemon kills its
                                          # bg SSH sessions and signals itself to exit
bash mcp/cluster-exec/cedaemon --status   # is it up?
bash mcp/cluster-exec/cedaemon --restart  # stop + start
```

`--stop` sends a bearer-authenticated POST to `/stop` on the server's HTTP port; the handler kills any backgrounded SSH children and schedules SIGTERM on itself, then returns 200. Liveness and stop both work over the network port, so they're robust across PID namespaces (e.g. codex/bwrap `--unshare-pid`) where pid tracking is unreliable. State lives in `./tmp/cluster-exec.{pid,log}` (the pid file is diagnostic only — never read for liveness or killing). The server runs in its own session (`setsid`) so it survives the parent shell exiting. Env vars (`CE_BASTION`, …) are read at startup; pass them through the shell:

```bash
CE_BASTION="bastion.example.com" bash mcp/cluster-exec/cedaemon --start
```

## Security warning

This tool grants the LLM **whatever SSH access your local `ssh` config has to `$CE_BASTION` and from there to `<hostname>`**. There are no allowlists or confirmation steps — the privilege is intentional, and the human running opencode is the admin. As a best-effort guard that steers the agent toward the supported primitives, the `run` tool rejects a handful of top-level commands (a message tells the agent what to use instead):

- `ssh` — use the `hostname` argument instead.
- `head` / `tail` — only `head -1` / `tail -1` are allowed in-command; to truncate the returned output use the `run()` `head_lines` / `tail_lines` args instead.
- `headtail` — never allowed.
- `sleep` longer than 10s.

This is advisory, not adversarial: nested content (quoted strings, `$(...)` subshells, backticks) is not inspected. The HTTP transport binds to `127.0.0.1` only; do not change `CE_HTTP_HOST` to expose it.

### Bearer authentication

Every request must include `Authorization: Bearer <token>`. The token is read from a single file (default `~/.config/cluster-exec/bearer`, mode 0600) — the path is printed on `--start` and logged at server startup. The file is created with a fresh 256-bit random token on first startup and reused across restarts (deleting the file rotates the token on the next start).

`cedaemon --start` auto-adds `export CE_BEARER="$(cat '<bearer_path>')"` to `.envrc` (default `$REPO/.envrc`, override with `CE_ENVRC`) so opencode's `{env:CE_BEARER}` header substitution picks it up — see `opencode.json`:

```json
{
  "mcp": {
    "cluster_exec": {
      "type": "remote",
      "url": "http://127.0.0.1:4003/mcp",
      "headers": { "Authorization": "Bearer {env:CE_BEARER}" }
    }
  }
}
```

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

Or call the `tail` MCP tool — same idea, but the events are pre-rendered as a human-readable transcript (one line per event with timestamp, session, type, and body) instead of raw JSON. Output looks like:

```
[tail] 4 events from tmp/cluster.log (tail=50)

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

## Backgrounded runs

Long-running commands auto-background after `auto_bg_after_s`. They keep executing on the cluster (the SSH session is alive on the cluster node) and continue emitting lines to the JSONL log. To work with them:

```text
run    → returns "BACKGROUNDED session=abcd1234 after 30.0s"
tail   → render the latest transcript (pass session=abcd1234 to filter)
status → see all sessions (running and exited), with elapsed time and hard-timeout countdown
kill   → terminate by session_id
```

Backgrounded runs are still subject to `timeout_s` (the hard timeout). The `timeout_watcher` is registered when a run backgrounds; when it fires, the proc + readers are terminated and the session is marked exited in the registry.

The registry is persisted to `$CE_STATE_PATH` (default
`tmp/cluster-exec.state.json`, mode 0600) on every mutation and reloaded
when the daemon starts. A daemon restart therefore does not erase `status`:
finished runs keep their exit state, and runs that were still in flight are
tagged `orphaned-by-restart`. `tail -s <id>` keeps working from the JSONL
log, and `kill` finds a recovered local ssh child via its
`CE_SESSION=<id>` argv marker, then sends SIGTERM and escalates to SIGKILL.
`cedaemon --stop` terminates live children
but retains their terminal records, so `--restart` keeps the history too.
There is no automatic reaper yet; the state file is small and can be
deleted when it gets noisy.

## SSH connection reuse

All `run`, `put`, and `get` calls share an SSH ControlMaster connection to
`$CE_BASTION`. The first call after the socket expires pays the normal
handshake; subsequent calls multiplex over the existing connection and avoid
it. In practice this is a ~3s to ~0.3s difference per call on a typical
bastion.

The master persists for `CE_SSH_CONTROL_PERSIST_S` seconds (default 300)
after its last client exits. The socket defaults to
`~/.ssh/cluster-exec-%C`; OpenSSH expands `%C` to a hash of the destination,
user, and port, so different bastions don't collide. Keep the path short
(the Unix socket limit is typically 104 bytes) and in a directory owned by
your user. `cedaemon --stop` explicitly closes the master with
`ssh -O exit`, so stopping cluster-exec does not leave a bastion connection
alive for the rest of the persist window. Set `CE_SSH_CONTROL_PERSIST_S=0`
to disable multiplexing manually. If the parent directory is missing, not
owned by your user, group/world-writable, or the path is too long for a Unix
socket, cluster-exec logs a warning at startup and runs without multiplexing
rather than breaking SSH commands.

Only the outer hop is multiplexed. The inner `ssh <target>` still runs from
the bastion for each `run` call, and file transfers keep their own binary
stdin/stdout channel over the shared connection.

Two operational trade-offs: the bastion's `MaxSessions` limit (10 by default
on OpenSSH) bounds concurrent multiplexed calls, and a dropped master fails
all in-flight sessions at once. For normal sequential agent use this is a
large latency win; disable it with `CE_SSH_CONTROL_PERSIST_S=0` if either
trade-off matters more than latency.

## Ansible

Run playbooks via `hostname="localhost"` to skip the inner ssh and land directly on the bastion (which is also `$YC_SYNC_HOST` and typically the dev-sync host). `/etc/ansible` is mounted on every core node — the entry has it too:

```bash
run(command="cd /etc/ansible && ./run-playbook.sh admin/install-ycluster-package.yml", hostname="localhost")
run(command="cd /etc/ansible && ansible-playbook admin/install-ycluster-package.yml --limit s3", hostname="localhost")
```

`./run-playbook.sh` sources `env.sh` (vault password + LC_ALL) and is the preferred entry point per the project's AGENTS.md. Playbooks that legitimately run longer than 10s will be returned as `BACKGROUNDED session=<id>`; poll with `tail -s <id>` until the end event lands, or `status` to see the hard-timeout countdown. To hold a specific deploy in the foreground instead, restart the daemon with `CE_AUTO_BG_TIMEOUT_S=120` (or whatever fits). `timeout_s` defaults to 1800s — fine for most deploys.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `CE_BASTION` | (required) | Entry host the outer ssh hops through. Should resolve in your local `~/.ssh/config`. |
| `CE_SSH_CONTROL_PERSIST_S` | `300` | Reuse one SSH connection to the bastion via ControlMaster for this many seconds after the last client exits. `0` disables multiplexing. |
| `CE_SSH_CONTROL_PATH` | `~/.ssh/cluster-exec-%C` | Control socket path. `%C` expands to a hash of destination/user/port; keep it short and in a user-owned directory. Empty disables multiplexing. |
| `CE_HTTP_HOST` | `127.0.0.1` | Bind address for the streamable-HTTP transport. Loopback only by default — admin tool grants root SSH. |
| `CE_HTTP_PORT` | `4003` | Bind port. |
| `CE_HTTP_PATH` | `/mcp` | MCP endpoint path. |
| `CE_BEARER_PATH` | `~/.config/cluster-exec/bearer` | Bearer token file (mode 0600). Created on first start; survives restarts. |
| `CE_ENVRC` | `$REPO/.envrc` | `.envrc` file that `--start` auto-appends `export CE_BEARER=…` to. |
| `CE_LOG_PATH` | `tmp/cluster.log` | JSONL log path (relative to MCP server CWD). |
| `CE_STATE_PATH` | `$CE_LOG_PATH` directory + `cluster-exec.state.json` | Persisted background-session registry (mode 0600). Reloaded on daemon start so `status` survives restarts. |
| `CE_TIMEOUT_S` | `1800` | Per-call hard timeout in seconds on backgrounded runs (SIGTERM, then SIGKILL after 2s). Set to 0 to disable. Must be greater than `CE_AUTO_BG_TIMEOUT_S` when enabled. |
| `CE_AUTO_BG_TIMEOUT_S` | `10` | Auto-bg threshold in seconds — `run` tool calls return with `BACKGROUNDED session=<id>` after this many seconds of foreground time. Bump (e.g. `120`) for known-long commands; set to 0 to disable auto-backgrounding (foreground blocks until `timeout_s`). |
| `CE_PROGRESS_INTERVAL_S` | `1.0` | Progress notification cadence in seconds. |
| `CE_PROGRESS_TAIL_LINES` | `50` | Lines of cumulative tail shown in each progress notification. |
| `CE_TAIL_STATUS_MAX_CALLS` | `3` | Max `tail`/`status` calls per session inside the window; beyond this, calls are refused with a backoff recommendation. |
| `CE_TAIL_STATUS_WINDOW_S` | `20` | Sliding window (seconds) over which the `tail`/`status` rate limit counts calls. |
| `CE_TAIL_STATUS_RECOMMENDED_SLEEP_S` | `10` | Minimum backoff (seconds) an agent is told to sleep after a rate-limited `tail`/`status` call. |
| `CE_CAPTURE_REQUESTS` | unset | Debug-only: when set to a file path, every incoming HTTP request is appended to that path as one JSONL line (ts, method, path, headers, body). The bearer token is redacted to `Bearer ***` before write; codex `x-codex-turn-metadata.workspaces[*].associated_remote_urls` is reduced to hostname only and `latest_git_commit_hash` to `***`. Use to inspect what an MCP client actually sends on the wire. |

## Development

Run the tests:

```bash
cd mcp/cluster-exec
PYTHONPATH=src python3 -m unittest tests.test_exec tests.test_bg_registry tests.test_state tests.test_log tests.test_truncate tests.test_ratelimit tests.test_cli tests.test_auth tests.test_file_transfer tests.test_schema tests.test_stop_endpoint tests.test_control_path
```

Smoke-test the daemon (then trigger `run` from opencode):

```bash
CE_BASTION="bastion.example.com" bash mcp/cluster-exec/cedaemon --start
bash mcp/cluster-exec/cedaemon --status
```
