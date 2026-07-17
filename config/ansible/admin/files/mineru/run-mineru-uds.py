#!/usr/bin/python3.12
# Copyright (c) Opendatalab. All rights reserved.
# Launcher for mineru-api on a Unix domain socket instead of TCP.
#
# Used under podman's --network=none so the container has no netns
# interfaces at all — no socket(AF_INET, ...) syscall can succeed,
# and a crafted PDF that triggers an outbound fetch (pypdf /
# pdfplumber resolving external image URIs, font fetches, etc.)
# fails at the syscall layer rather than relying on mineru's
# application-level SSRF mitigation in
# mineru/cli/public_http_client_policy.py (which only blocks
# attacker-supplied server_url / *-http-client backends).
#
# Flow:
#   container (--network=none, no AF_INET)
#     mineru-api --host 0.0.0.0 --port 8000 --enable-vlm-preload true
#       ↓ (this wrapper monkey-patches uvicorn.Config to bind a UDS)
#     UDS at /run/mineru/mineru.sock (filesystem, not networking —
#                                    works under --network=none)
#   host (socat sidecar in mineru-podman-proxy.service)
#     socat TCP-LISTEN:8000,reuseaddr,fork UNIX-CONNECT:/run/mineru/mineru.sock
#       ↓ (this is what local-ai-proxy + nginx reach)
#     http://<host>.xc:8000/file_parse (unchanged from the cluster's POV)
#
# Env vars consumed:
#   - MINERU_UDS_PATH (required): UDS path, e.g. /run/mineru/mineru.sock.
#     Set by mineru-podman.service via `podman run -e`.
#   - HF_HOME, MINERU_MODEL_SOURCE, MINERU_API_MAX_CONCURRENT_REQUESTS:
#     read by mineru.cli.fast_api directly, pass through unchanged.
#
# CLI args (forwarded to mineru.cli.fast_api.main verbatim):
#   --host <host>, --port <port>, --enable-vlm-preload <bool>, ...
#   host/port are accepted (mineru-api's CLI requires them and they're
#   used in the startup banner / SSRF policy warning), but uvicorn
#   ignores them when uds is set.

import errno
import os
import socket
import stat
import sys

# Bail loudly before we touch anything else if MINERU_UDS_PATH is unset.
# Without it the wrapper would silently fall through to a TCP bind, which
# would fail under --network=none with a confusing ENETUNREACH much later
# in uvicorn startup — much harder to debug than this early check.
UDS_PATH = os.environ.get("MINERU_UDS_PATH")


def _preflight_uds(path: str) -> None:
    """Self-recover from stale UDS files; refuse to start on live ones.

    The systemd unit binds /run/mineru/mineru.sock (UDS) inside the
    container. /run/mineru is host-side tmpfs owned by `dev`, so the
    UDS file persists across `podman run --rm` cycles — a crash that
    exits before uvicorn cleans up the socket leaves a stale file
    on the host, and the next container start hits a bind collision.

    uvicorn 0.51.0 does NOT raise EADDRINUSE on UDS collision —
    it silently hangs at "Waiting for application startup", which
    would wedge systemd's Restart=always loop. So the wrapper
    does the check up front:

    1. If the path doesn't exist → proceed (normal first start).
    2. If the path exists but is not a socket (regular file,
       directory, symlink to something else) → refuse; something
       unexpected is squatting on the path. Better to fail loudly
       than silently delete a non-socket.
    3. If the path exists and IS a socket:
       - Try connect(). If connect succeeds → another process is
         actively listening on the UDS. Refuse (likely a duplicate
         service or stale listener; systemd's RestartPreventExitStatus
         won't help here because we're exiting before uvicorn does).
       - If connect fails with ECONNREFUSED → no listener; the file
         is stale. Unlink and proceed.
       - If connect fails with EACCES/EPERM → refuse (permission
         mismatch; the wrapper runs as the container's `dev` user
         and shouldn't be silently fixing ACL drift).

    The check + unlink is racy (a second process could bind the path
    between our unlink and uvicorn's bind). The window is microseconds
    and the only writer is this unit, so in practice it's safe. If a
    race does happen, the unit's Restart=always covers it (uvicorn
    will at worst hang briefly before systemd kills it via the
    TimeoutStartSec watchdog — see mineru-podman.service).
    """
    if not os.path.exists(path):
        return
    st = os.lstat(path)
    if not stat.S_ISSOCK(st.st_mode):
        sys.stderr.write(
            f"run-mineru-uds: {path} exists but is not a socket "
            f"(mode={oct(st.st_mode)}); refusing to start.\n"
            "  Refusing to unlink: the path is occupied by something "
            "unexpected (file, directory, symlink). Remove manually.\n"
        )
        sys.exit(3)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except ConnectionRefusedError:
        # Stale socket file, no listener. Clean up and proceed.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass  # raced with another unlink — fine
        sys.stderr.write(
            f"run-mineru-uds: removed stale socket at {path} "
            "(no listener; leftover from a previous crash).\n"
        )
        return
    except (PermissionError, OSError) as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            sys.stderr.write(
                f"run-mineru-uds: cannot connect to {path}: "
                f"{e.strerror}; refusing to start.\n"
                "  Permission mismatch on the UDS path — investigate "
                "before restart, do not let the wrapper fix ACLs.\n"
            )
            sys.exit(3)
        raise
    else:
        # Connect succeeded — someone IS listening. Refuse.
        probe.close()
        sys.stderr.write(
            f"run-mineru-uds: {path} is already in use by another "
            "process; refusing to start.\n"
            "  Likely a duplicate mineru-podman.service instance, or "
            "a stale socat child holding the socket. Check:\n"
            "    ss -lx | grep mineru.sock\n"
            "    systemctl status mineru-podman.service\n"
        )
        sys.exit(3)
    finally:
        try:
            probe.close()
        except OSError:
            pass


def _patch_uvicorn_config() -> None:
    """Force uvicorn.Config to bind a Unix socket instead of host:port.

    Monkey-patches uvicorn.Config.__init__ so every Config construction
    (both `uvicorn.run(...)` and the explicit `uvicorn.Config(...) +
    uvicorn.Server(config).run()` path that mineru.cli.fast_api uses
    in non-reload mode) inherits the uds value from MINERU_UDS_PATH.

    Must run BEFORE mineru.cli.fast_api is imported, because that
    module pulls uvicorn at the top and mineru.cli.fast_api.main
    constructs the Config at call time — patching the already-imported
    uvicorn module works because monkey-patching modifies the actual
    class object, not a per-call copy.
    """
    import uvicorn  # local import: this module is only available in the container image

    original_init = uvicorn.Config.__init__

    def patched_init(self, *args, **kwargs):
        # Force uds to MINERU_UDS_PATH regardless of what the caller
        # passed. uvicorn ignores host/port when uds is set, so we
        # don't need to clear them — but doing so keeps the banner
        # honest if anyone inspects the running Config.
        kwargs["uds"] = os.environ["MINERU_UDS_PATH"]
        original_init(self, *args, **kwargs)

    uvicorn.Config.__init__ = patched_init


_patch_uvicorn_config()

# Hand off to mineru.cli.fast_api.main. Click uses sys.argv[1:] for
# parsing when main() is called without an explicit argument list, so
# we rename argv[0] to "mineru-api" to keep the startup banner honest
# ("Start MinerU FastAPI Service: ...").
#
# The `if __name__ == "__main__":` guard is mandatory: vllm's
# AsyncLLM uses multiprocessing.spawn to launch the EngineCoreProc
# subprocess, and spawn re-imports the parent's __main__ module
# to obtain the target callable. Without this guard, the wrapper
# would re-execute main() in the spawned child, which would in turn
# call preload_vlm_model → AsyncLLM.__init__ → spawn again — infinite
# recursion detected by Python's `_check_not_importing_main()` and
# raised as "An attempt has been made to start a new process before
# the current process has finished its bootstrapping phase." The
# guard makes __main__ safely re-importable; the uvicorn monkey-patch
# above still runs at import time, which is what we want — any
# process that imports the wrapper (including vllm-spawned workers
# that happen to start a uvicorn for their own serving API) gets
# the patched uvicorn and binds the UDS instead of TCP.
#
# The UDS pre-flight runs INSIDE the guard, not at import time, so
# spawn workers (e.g. mineru's render executor workers, which
# re-import the wrapper) don't trigger its "connect succeeds →
# sys.exit(3)" branch and die silently → BrokenProcessPool on the
# parent. Only the FastAPI server (the actual uvicorn binder) needs
# to check for a duplicate UDS listener.
if __name__ == "__main__":
    _preflight_uds(UDS_PATH)
    sys.argv = ["mineru-api", *sys.argv[1:]]
    from mineru.cli.fast_api import main  # noqa: E402
    main()
