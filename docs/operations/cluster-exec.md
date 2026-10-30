# Cluster exec via MCP

`mcp/cluster-exec` is a local MCP server exposing four tools: `run` (execute
a shell command on an internal host via the two-hop SSH pattern), `tail`
(render the tail of the JSONL log as a human-readable transcript), `status`
(list or inspect backgrounded runs), and `kill` (terminate a backgrounded
run by session_id). It runs as a streamable-HTTP daemon on
`http://127.0.0.1:4003/mcp` and is registered in `opencode.json` as a
`remote` MCP. The user handles starting it.

## Prod only

`run` hops through `$CE_BASTION` to prod hosts — the dev cluster
(`dev/cluster/`, local Incus) and `system-test.sh` runs are not reachable
through it. For those surfaces use `ssh -F dev/cluster/.ssh/config` or run
Ansible from `dev/cluster/` directly.

## Usage

Use `hostname="localhost"` for anything that should run on the bastion
itself, e.g. Ansible playbooks:

```text
run(command="cd /etc/ansible && ./run-playbook.sh admin/install-ycluster-package.yml",
    hostname="localhost")
```

Avoid the use of `ssh` inside the command — specify the hostname as a tool
argument instead.

## SSH connection reuse

`run`, `put`, and `get` multiplex over one SSH connection to
`$CE_BASTION`. The first call after the control socket expires pays the
handshake; later calls reuse it. The socket defaults to
`~/.ssh/cluster-exec-%C` (the `%C` token hashes the destination, user, and
port) and the master persists for 300s after its last client. Tune with
`CE_SSH_CONTROL_PERSIST_S`; set it to `0` to disable multiplexing. Override
`CE_SSH_CONTROL_PATH` if the default path is too long for `sockaddr_un`
(typically 104 bytes) or lands in an unwritable directory. An unusable
control path is reported at startup and multiplexing is disabled; SSH
commands still run.

Only the outer hop is shared. Each remote command still runs `ssh <target>`
from the bastion. Concurrent calls are bounded by the bastion's OpenSSH
`MaxSessions` (10 by default), and loss of the master fails all in-flight
sessions on that connection. `cedaemon --stop` closes the master with
`ssh -O exit` after terminating background sessions.

## Backgrounding

Foreground is capped at 10s by default (`CE_AUTO_BG_TIMEOUT_S`) — the `run`
tool call always returns within 10s, either with the result or with
`BACKGROUNDED session=<id>`. The default workflow is to poll with `status`
+ `tail -s <id>` / `kill` after it backgrounds. Bump the env var (e.g.
`CE_AUTO_BG_TIMEOUT_S=120 bash mcp/cluster-exec/cedaemon --start`) to hold a
known-long command in the foreground rather than auto-backgrounding.

`tail` and `status` are rate-limited per session (max
`CE_TAIL_STATUS_MAX_CALLS`, default 3 calls per `CE_TAIL_STATUS_WINDOW_S`,
default 20s). A call over the limit is refused with a `RATE-LIMITED: …`
message recommending the agent sleep (at least
`CE_TAIL_STATUS_RECOMMENDED_SLEEP_S`, default 10s) before polling again — so
pace polls instead of busy-looping while a long command runs.

Full stdout/stderr is logged (untruncated) to `./tmp/cluster.log` as JSONL
events; the tool result returns only the head/tail (defaults 25/25) of the
chronologically merged stream, with `[stderr]` tags. Progress notifications
tick every second while a long command runs.

The hard backstop on the backgrounded process itself is `timeout_s` (default
1800s, `CE_TIMEOUT_S`; SIGTERM, then SIGKILL after 2s). If the HTTP client
disconnects mid-`run`, the SSH proc is still registered as a backgrounded
session — find it with `status` (no args) or `tail -s <id>` and recover with
`kill`, or it gets cleaned up by the `timeout_s` watcher or `cedaemon --stop`.

The background-session registry is persisted to `$CE_STATE_PATH` (default
`tmp/cluster-exec.state.json`, mode 0600) and reloaded on daemon start, so a
restart does not erase `status`. Restored in-flight sessions are tagged
`orphaned-by-restart`; `kill` finds the surviving local ssh child by its
`CE_SESSION=<id>` argv marker, then SIGTERMs it and escalates to SIGKILL.
`cedaemon --stop` terminates live children but keeps
their terminal records for the next daemon process. There is no automatic
reaper yet.

## Session visibility

Sessions are keyed by their `session_id`, and by-ID access is not scoped to
the caller's thread: `status -s <id>` reports any run on this server, and
`kill -s <id>` terminates any run, so an agent that is handed a
`session_id` (e.g. by a sub-agent) can inspect and kill it. `tail -s <id>`
is the read-only transcript for any session.

The listing is the one scoped path: `status` with no argument lists only
the backgrounded runs owned by the caller's thread (filtered by the
caller's `_meta.threadId`, falling back to the `mcp-session-id` header), so
you can't enumerate another thread's runs without being handed an id.

`_meta.threadId` is the conventional MCP slot for a stable per-process session
identifier — codex puts its thread ID there on every `tools/call`, so the
principal survives a transport-level reconnect within the same codex
invocation. Clients that don't populate `_meta.threadId` (e.g. opencode) fall
back to the `mcp-session-id` header, which is per-connection — reused across
reconnects within one process, but the server's session manager can expire it
independently, and a fresh process invocation (opencode restart, codex
resume) gets a new ID and cannot see prior orphans. Either way, orphans are
still readable via `status`/`tail -s <id>` and get cleaned up by the hard
`timeout_s` watcher or by `cedaemon --stop`.

## `sudo -u <user>` quirk on nvidia hosts

cluster-exec lands as root, so `sudo -u dev podman …` would normally work, but
sudo's `env_reset` tries to chdir to the invoker's home (`/root`), which fails
with `cannot chdir to /root: Permission denied`. Workaround: prefix the
command with `cd /` so sudo has nowhere to chdir:

```text
run(command="cd / && sudo -u dev podman ps --filter name=ds4",
    hostname="nv4")
```

Applies to any prod host where the target user (e.g. `dev`) doesn't have read
access to the invoker's home.

## `exec_command` background terminals

When `exec_command` returns a `session_id` (or the response body says
"Process running with session ID N"), the shell is still running in the
background. Reuse it via `write_stdin(session_id=N)` to read more output or
send input, or terminate it. Do not re-run the same command — that leaks the
background process. An empty `output` field does not mean the command
finished; check `session_id`/`exit_code` (or the "Process running"/"Process
exited" line) before deciding. Background terminals persist across turns.
