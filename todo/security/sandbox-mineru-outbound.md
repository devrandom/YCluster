# Sandbox MinerU's outbound at the OS layer

Today the only SSRF mitigation on MinerU is its application-level disable
of `*-http-client` backends when bound to 0.0.0.0 (see
`mineru/cli/public_http_client_policy.py`). That blocks attacker-supplied
`server_url` / `backend: *-http-client` fields, but does not stop a crafted
PDF from triggering outbound fetches via other parts of the pipeline
(pypdf / pdfplumber resolving external image URIs, font fetches, etc.).

We keep the 0.0.0.0 bind by convention (proxy reaches the backend
cluster-internal, low risk for inbound) but add a hard OS-level outbound
block on **both** deploy platforms.

## macOS (m\*)

Wrap `launchd` `ProgramArguments` with `sandbox-exec -f sandbox.sb` and a
Seatbelt profile that denies all `network-out` except loopback.

- `config/ansible/macos/files/mineru/run-mineru.sh` — already binds
  `--host 0.0.0.0`, leave as-is.
- new `config/ansible/macos/files/mineru/sandbox.sb` — `(version 1) (deny
  default) (allow network-bind (local ip "127.0.0.1:*")) (allow network-in
  (local ip "127.0.0.1:*")) (deny network-out)` plus the resource access
  rules MinerU needs (filesystem reads of model cache, HF home, etc.).
- `config/ansible/macos/setup-mineru.yml` — change `ProgramArguments` to
  `["/usr/bin/sandbox-exec", "-f", "/usr/local/etc/mineru.sb",
  "/Users/dev/mineru-venv/bin/mineru-api", ...]`, copy `sandbox.sb` to
  `/usr/local/etc/mineru.sb`.

## Linux/Nvidia (nv\*)

Add `--network=none` to `mineru-podman.service`. The container has no
netns interfaces at all, so no `socket(AF_INET,...)` syscall can succeed.
To keep the API reachable, `mineru-api` binds to a Unix socket at
`/run/mineru/mineru.sock` (filesystem, not networking — works under
`--network=none`), and a sidecar `socat` process listens on host TCP:8000
and bridges to the socket. Kernel-enforced, no firewall rules need to be
maintained.

- new `config/ansible/admin/files/mineru/run-mineru-uds.py` — monkey-patches
  `uvicorn.Config` / `uvicorn.run` to bind a Unix socket, then invokes
  `mineru.cli.fast_api.main()` unchanged.
- `config/ansible/admin/files/mineru/Containerfile` — change ENTRYPOINT to
  invoke the UDS wrapper.
- `config/ansible/admin/files/mineru/mineru-podman.service` — add
  `--network=none`, drop `-p 8000:8000`, add `-v /run/mineru:/run/mineru`.
- new `config/ansible/admin/files/mineru/mineru-podman-proxy.service` —
  runs `socat TCP-LISTEN:8000,reuseaddr,fork
  UNIX-CONNECT:/run/mineru/mineru.sock`, `After=mineru-podman.service`.
- `config/ansible/admin/install-mineru.yml` — install `socat` apt dep,
  install proxy unit, pre-create `/run/mineru/`.

## Status

DONE for Linux/Nvidia (commit `e5ecb1b`). The post-deployment fixes
(RuntimeDirectory chown, Containerfile COPY --chown, wrapper shebang, vllm
spawn guard, dev UID pre-flight) are documented in
`docs/design/mineru-podman.md` so the next operator doesn't re-debug them.
**macOS Seatbelt profile still TBD.**

## Follow-up regressions found after the redeploy

Both fixed and rolled into the next redeploy.

### BrokenProcessPool on `/file_parse` (fix #6)

The UDS pre-flight ran at module-import time and killed mineru's render
executor spawn workers (whose re-import of the wrapper hits the "UDS
already in use → exit 3" branch). Fix: move the pre-flight inside the
`if __name__ == "__main__":` guard. Verified by live `/file_parse` returning
`status=completed` in ~0.5 s (`backend=pipeline`) and ~18 s
(`backend=vlm-engine`).

### Layer squash pushes 11 GB on every wrapper tweak (fix #7)

`buildah bud` in 1.33 squashes by default, so the wrapper change
invalidated the entire ~11 GB final layer. Fix: `buildah bud --layers …` in
`install-mineru.yml`. Image now has ~9 OCI layers; a wrapper-only change
pushes layers 6-8 (a few KB) in seconds instead of ~6 min for the whole 11
GB tarball.

## Test before commit

Model is already pre-downloaded so the venv and HF cache lookups must
work under the sandbox. If a path is missing, allow it explicitly in the
profile / wrapper.
