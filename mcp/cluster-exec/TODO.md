# TODOs

Open work for cluster-exec. Move items here when deferring — keeps the
codebase free of in-source `# TODO` clutter while preserving the
rationale.

## client-isolation for backgrounded sessions

`_BACKGROUNDED` (`src/cluster_exec/exec.py`) is a single module-level
dict; the bearer token is one static secret (`auth.StaticTokenVerifier`,
`server.py`). Every authenticated caller can see every session via
`status` and terminate any session via `kill` — including ones started
by other clients.

Today this is acceptable: the daemon binds to `127.0.0.1:4003` and the
bearer file lives at `~/.config/cluster-exec/bearer` with mode 0600, so
in practice only the local user running opencode has access. It becomes
a real gap if the daemon is ever exposed beyond localhost.

Fix shape: add a `principal` field on `_Backgrounded` populated from the
bearer token (or a per-client sub-token), and filter in
`list_backgrounded` / `get_backgrounded` so `status` and `kill` only
operate on the caller's own sessions.
