import asyncio
import shlex
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import log
from .truncate import truncate

ProgressFn = Callable[[int, str], Awaitable[None]]


@dataclass
class _Backgrounded:
    session_id: str
    target_host: str
    command: str
    started: float
    timeout_s: float
    proc: asyncio.subprocess.Process
    readers: tuple[asyncio.Task, asyncio.Task]
    timeout_watcher: asyncio.Task | None = None
    orphaned: bool = False


_BACKGROUNDED: dict[str, _Backgrounded] = {}


def list_backgrounded() -> list[str]:
    return list(_BACKGROUNDED.keys())


def get_backgrounded(session_id: str) -> _Backgrounded | None:
    return _BACKGROUNDED.get(session_id)


def kill_backgrounded(session_id: str, grace_s: float = 2.0) -> bool:
    bg = _BACKGROUNDED.pop(session_id, None)
    if bg is None:
        return False
    if bg.timeout_watcher is not None and not bg.timeout_watcher.done():
        bg.timeout_watcher.cancel()
        try:
            await_cancelled(bg.timeout_watcher)
        except Exception:
            pass
    if bg.proc.returncode is None:
        bg.proc.terminate()
        try:
            deadline = time.monotonic() + grace_s
            while time.monotonic() < deadline and bg.proc.returncode is None:
                time.sleep(0.05)
            if bg.proc.returncode is None:
                bg.proc.kill()
        except Exception:
            pass
    return True


def kill_all_backgrounded(grace_s: float = 2.0) -> int:
    # Snapshot keys: kill_backgrounded mutates the dict as it pops.
    count = 0
    for sid in list(_BACKGROUNDED.keys()):
        if kill_backgrounded(sid, grace_s=grace_s):
            count += 1
    return count


def await_cancelled(task: asyncio.Task) -> None:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return
    try:
        loop.run_until_complete(task)
    except BaseException:
        pass


def register_backgrounded(
    session_id: str,
    target_host: str,
    command: str,
    started: float,
    timeout_s: float,
    proc: asyncio.subprocess.Process,
    readers: tuple[asyncio.Task, asyncio.Task],
    timeout_watcher: asyncio.Task,
    orphaned: bool = False,
) -> None:
    _BACKGROUNDED[session_id] = _Backgrounded(
        session_id=session_id,
        target_host=target_host,
        command=command,
        started=started,
        timeout_s=timeout_s,
        proc=proc,
        readers=readers,
        timeout_watcher=timeout_watcher,
        orphaned=orphaned,
    )


def _drop_backgrounded(session_id: str) -> None:
    _BACKGROUNDED.pop(session_id, None)


async def run(
    *,
    bastion: str,
    target_host: str,
    command: str,
    session_id: str,
    timeout_s: float,
    auto_bg_after_s: float,
    progress_interval_s: float,
    progress_tail_lines: int,
    head_lines: int,
    tail_lines: int,
    cwd: str,
    user: str | None = None,
    on_progress: ProgressFn | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    if cancel is None:
        cancel = asyncio.Event()
    if auto_bg_after_s > 0 and timeout_s > 0 and timeout_s <= auto_bg_after_s:
        raise ValueError(
            f"timeout_s ({timeout_s}) must be greater than auto_bg_after_s ({auto_bg_after_s})"
        )

    if target_host == "localhost":
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            bastion,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        inner = f"ssh {shlex.quote(ssh_target)} {shlex.quote(command)}"
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            bastion,
            inner,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    assert proc.stdout is not None and proc.stderr is not None

    stdout_buf: list[tuple[float, str]] = []
    stderr_buf: list[tuple[float, str]] = []
    started = time.monotonic()

    log.start(session_id, target_host, command, cwd)

    async def reader(stream: asyncio.StreamReader, tag: str, buf: list) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            buf.append((time.monotonic(), text))
            log.line(session_id, tag, text)

    r_out = asyncio.create_task(reader(proc.stdout, "stdout", stdout_buf))
    r_err = asyncio.create_task(reader(proc.stderr, "stderr", stderr_buf))
    readers = (r_out, r_err)

    progress_task: asyncio.Task | None = None
    if on_progress is not None:

        async def emit_progress() -> None:
            while True:
                await asyncio.sleep(progress_interval_s)
                merged = sorted(stdout_buf + stderr_buf, key=lambda x: x[0])
                tail = merged[-progress_tail_lines:]
                stderr_set = {(ts, t) for ts, t in stderr_buf}
                body = "\n".join(
                    f"[stderr] {t}" if (ts, t) in stderr_set else t for ts, t in tail
                )
                try:
                    await on_progress(len(stdout_buf) + len(stderr_buf), body)
                except Exception:
                    pass

        progress_task = asyncio.create_task(emit_progress())

    async def terminate_proc() -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def cancel_watcher() -> None:
        try:
            await cancel.wait()
        except asyncio.CancelledError:
            return
        await terminate_proc()

    watcher_task = asyncio.create_task(cancel_watcher())

    wait_task = asyncio.create_task(proc.wait())
    bg_task: asyncio.Task | None = (
        asyncio.create_task(asyncio.sleep(auto_bg_after_s))
        if auto_bg_after_s > 0
        else None
    )
    timeout_task: asyncio.Task | None = (
        asyncio.create_task(asyncio.sleep(timeout_s)) if timeout_s > 0 else None
    )

    async def finalize_backgrounded() -> None:
        # Drop the session so `status` no longer lists it, and cancel the
        # leak-prone watchdogs (cancel_watcher waits on cancel forever if
        # nothing sets it; progress_task emits MCP notifications every
        # progress_interval_s even though the request stream is closed).
        # Idempotent — safe to call from either timeout_watcher_fn branch.
        _drop_backgrounded(session_id)
        cancel.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except BaseException:
            pass
        if progress_task is not None:
            progress_task.cancel()
            try:
                await progress_task
            except BaseException:
                pass

    async def timeout_watcher_fn() -> None:
        # Race the hard timeout against the proc exiting naturally. When
        # either fires, terminate the proc if still alive (hard-timeout
        # path) and finalize the session (drop + cancel watchdogs). The
        # wait_task is the one already running in the background — it
        # completes when proc.wait() returns, i.e. when the SSH child
        # exits for any reason (clean exit, SIGTERM/SIGKILL from us).
        elapsed = time.monotonic() - started
        remaining = timeout_s - elapsed
        if remaining > 0:
            timeout_inner = asyncio.create_task(asyncio.sleep(remaining))
            done, _ = await asyncio.wait(
                {timeout_inner, wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            timeout_inner.cancel()
            try:
                await timeout_inner
            except BaseException:
                pass
            if wait_task in done:
                # Proc exited naturally before the hard timeout.
                await finalize_backgrounded()
                return
        # Hard timeout fired (or remaining <= 0). Proc is still alive
        # here (else wait_task would have been in done).
        if proc.returncode is None:
            await terminate_proc()
        await finalize_backgrounded()

    race_set: list[asyncio.Task] = [wait_task]
    if bg_task is not None:
        race_set.append(bg_task)
    if timeout_task is not None:
        race_set.append(timeout_task)

    try:
        done, _ = await asyncio.wait(race_set, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # Outer cancellation = HTTP client disconnected mid-run. The MCP
        # notifications/cancelled path sets the `cancel` Event instead, which
        # the cancel_watcher handles by calling terminate_proc (that path is
        # unchanged because we re-raise). Here we register the SSH proc as a
        # backgrounded session so the user can pick it up via `status` /
        # `tail -s <id>` / `kill` instead of orphaning the child on the
        # bastion. The HTTP response won't reach the client (the request is
        # gone) — the session ID lands in the JSONL end event and in `status`
        # (no args), which is how the user discovers it after interrupting.
        if proc.returncode is None and not cancel.is_set():
            timeout_watcher = asyncio.create_task(timeout_watcher_fn())

            register_backgrounded(
                session_id=session_id,
                target_host=target_host,
                command=command,
                started=started,
                timeout_s=timeout_s,
                proc=proc,
                readers=readers,
                timeout_watcher=timeout_watcher,
                orphaned=True,
            )

            duration = time.monotonic() - started
            merged = sorted(
                [(ts, False, t) for ts, t in stdout_buf]
                + [(ts, True, t) for ts, t in stderr_buf],
                key=lambda x: x[0],
            )
            out_lines = [f"[stderr] {t}" if is_stderr else t for _, is_stderr, t in merged]
            log.end(
                session_id,
                None,
                duration,
                "orphaned-by-disconnect",
                lines=len(out_lines),
                error=(
                    "HTTP client disconnected mid-run; pick up with "
                    "status/tail -s <id>/kill"
                ),
            )
        raise

    backgrounded = bg_task is not None and bg_task in done
    timed_out = timeout_task is not None and timeout_task in done

    if backgrounded:
        timeout_watcher = asyncio.create_task(timeout_watcher_fn())

        register_backgrounded(
            session_id=session_id,
            target_host=target_host,
            command=command,
            started=started,
            timeout_s=timeout_s,
            proc=proc,
            readers=readers,
            timeout_watcher=timeout_watcher,
        )
        cancel.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except BaseException:
            pass
        if progress_task is not None:
            progress_task.cancel()
            try:
                await progress_task
            except BaseException:
                pass

        duration = time.monotonic() - started
        merged = sorted(
            [(ts, False, t) for ts, t in stdout_buf] + [(ts, True, t) for ts, t in stderr_buf],
            key=lambda x: x[0],
        )
        out_lines = [f"[stderr] {t}" if is_stderr else t for _, is_stderr, t in merged]
        body = truncate(out_lines, head_lines, tail_lines)
        log.end(session_id, None, duration, "backgrounded", lines=len(out_lines))
        status_line = f"BACKGROUNDED session={session_id} after {duration:.1f}s \u2014 poll with tail -s {session_id}, inspect with status, kill with the `kill` tool"
        return f"{status_line}\n\n{body}" if body else status_line

    external_cancel = cancel.is_set()
    if not external_cancel:
        cancel.set()
    watcher_task.cancel()
    try:
        await watcher_task
    except BaseException:
        pass

    external_cancel = external_cancel and not timed_out
    if timed_out:
        await terminate_proc()

    try:
        await asyncio.wait_for(asyncio.gather(r_out, r_err), timeout=2.0)
    except TimeoutError:
        r_out.cancel()
        r_err.cancel()
        await asyncio.gather(r_out, r_err, return_exceptions=True)
    if progress_task is not None:
        progress_task.cancel()
        try:
            await progress_task
        except BaseException:
            pass

    duration = time.monotonic() - started

    merged = sorted(
        [(ts, False, t) for ts, t in stdout_buf] + [(ts, True, t) for ts, t in stderr_buf],
        key=lambda x: x[0],
    )
    out_lines = [f"[stderr] {t}" if is_stderr else t for _, is_stderr, t in merged]

    if proc.returncode is None:
        status = "error"
        status_line = "ERROR: process did not exit cleanly"
        exit_code: int | None = None
    elif external_cancel:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (cancelled by client)"
        exit_code = proc.returncode
    elif timed_out:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (timeout)"
        exit_code = proc.returncode
    elif proc.returncode == 0:
        status = "ok"
        status_line = f"OK exit=0 duration={duration:.1f}s"
        exit_code = 0
    else:
        status = "failed"
        status_line = f"FAILED exit={proc.returncode} duration={duration:.1f}s"
        exit_code = proc.returncode

    log.end(session_id, exit_code, duration, status, lines=len(out_lines))

    body = truncate(out_lines, head_lines, tail_lines)
    return f"{status_line}\n\n{body}" if body else status_line


# ---------------------------------------------------------------------------
# File transfer (put / get) — foreground only, no auto-backgrounding.
#
# Stream bytes through SSH stdin/stdout using ``cat`` on the remote end so the
# payload is binary-safe and never staged on the bastion. Hard timeout via
# ``timeout_s`` (SIGTERM, then SIGKILL after 2s) — but no soft auto-bg
# transition, since file transfers are bounded and we want an immediate
# success/failure answer.
#
# The remote command (``cat > <path>`` for put, ``cat <path>`` for get) is
# passed as a single shlex-quoted argv entry to ``ssh``. ssh concatenates its
# argv with spaces and sends the joined string to the bastion shell, which
# re-parses it; the single-quote wrapping keeps the ``>`` redirect intact so
# the bastion shell doesn't redirect its own ssh's stdout.
# ---------------------------------------------------------------------------


async def put_file(
    *,
    bastion: str,
    target_host: str,
    local_path: str,
    remote_path: str,
    session_id: str,
    timeout_s: float,
    cwd: str,
    user: str | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    """Upload ``local_path`` to ``target_host:remote_path`` via SSH bastion.

    Streams the file contents through SSH stdin (``cat > file`` on the
    remote end) so the file is never staged on the bastion. Binary-safe.
    Foreground only — never auto-backgrounds. Hard timeout via
    ``timeout_s`` (SIGTERM, then SIGKILL after 2s).
    """
    if cancel is None:
        cancel = asyncio.Event()

    local = Path(local_path)
    if not local.exists():
        return f"ERROR: local file not found: {local_path}"
    if not local.is_file():
        return f"ERROR: local path is not a regular file: {local_path}"

    # The command the *target* host should run. ``dd of=<path>`` reads stdin
    # and writes to the file (binary-safe). We use ``dd`` rather than
    # ``cat > <path>`` because the redirect ``>`` would be interpreted by
    # the bastion's shell on the two-hop path, not the target's — wrapping
    # it in single quotes to dodge that breaks the target shell, which then
    # treats the whole quoted string as one command name. ``dd`` has no
    # shell metacharacters, so the command string is safe to pass through
    # both hops without quoting tricks. ``dd`` writes its ``N+M records``
    # status to stderr — we drain stderr so the caller sees it via the
    # ``tail`` tool's transcript.
    target_cmd_str = f"dd of={shlex.quote(remote_path)}"
    quoted_target_cmd = target_cmd_str

    if target_host == "localhost":
        ssh_argv: list[str] = ["ssh", bastion, quoted_target_cmd]
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        ssh_argv = [
            "ssh", bastion,
            "ssh", shlex.quote(ssh_target), "--", quoted_target_cmd,
        ]

    cat_proc = await asyncio.create_subprocess_exec(
        "cat",
        str(local),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Use stdin=PIPE and pump bytes from cat_proc.stdout to ssh_proc.stdin
    # ourselves. Passing ``cat_proc.stdout`` (a StreamReader) directly as
    # stdin doesn't work — asyncio's create_subprocess_exec tries to call
    # ``fileno()`` on the stdin argument and StreamReader has none.
    ssh_proc = await asyncio.create_subprocess_exec(
        *ssh_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    cat_out: asyncio.StreamReader = cat_proc.stdout  # type: ignore[assignment]
    ssh_in: asyncio.StreamWriter = ssh_proc.stdin  # type: ignore[assignment]
    cat_err: asyncio.StreamReader = cat_proc.stderr  # type: ignore[assignment]
    ssh_err: asyncio.StreamReader = ssh_proc.stderr  # type: ignore[assignment]
    assert cat_out is not None and ssh_in is not None and cat_err is not None and ssh_err is not None

    started = time.monotonic()
    log.start(session_id, target_host, f"PUT {local_path} -> {remote_path}", cwd)

    bytes_sent = 0

    async def pump() -> None:
        nonlocal bytes_sent
        try:
            while True:
                chunk = await cat_out.read(8192)
                if not chunk:
                    break
                ssh_in.write(chunk)
                await ssh_in.drain()
                bytes_sent += len(chunk)
        except Exception:
            pass
        finally:
            try:
                if not ssh_in.is_closing():
                    ssh_in.close()
            except Exception:
                pass

    pump_task = asyncio.create_task(pump())

    async def drain(stream: asyncio.StreamReader, tag: str, buf: list[str]) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            buf.append(text)
            log.line(session_id, tag, text)

    cat_err_buf: list[str] = []
    ssh_err_buf: list[str] = []
    drain_cat = asyncio.create_task(drain(cat_err, "stderr", cat_err_buf))
    drain_ssh = asyncio.create_task(drain(ssh_err, "stderr", ssh_err_buf))

    async def terminate_all() -> None:
        if ssh_proc.returncode is None:
            ssh_proc.terminate()
            try:
                await asyncio.wait_for(ssh_proc.wait(), timeout=2.0)
            except TimeoutError:
                ssh_proc.kill()
                await ssh_proc.wait()
        if cat_proc.returncode is None:
            cat_proc.terminate()
            try:
                await asyncio.wait_for(cat_proc.wait(), timeout=2.0)
            except TimeoutError:
                cat_proc.kill()
                await cat_proc.wait()

    async def cancel_watcher() -> None:
        try:
            await cancel.wait()
        except asyncio.CancelledError:
            return
        await terminate_all()

    watcher_task = asyncio.create_task(cancel_watcher())
    wait_task = asyncio.create_task(ssh_proc.wait())
    timeout_task: asyncio.Task | None = (
        asyncio.create_task(asyncio.sleep(timeout_s)) if timeout_s > 0 else None
    )

    race_set: list[asyncio.Task] = [wait_task, watcher_task]
    if timeout_task is not None:
        race_set.append(timeout_task)

    done, _ = await asyncio.wait(race_set, return_when=asyncio.FIRST_COMPLETED)

    timed_out = timeout_task is not None and timeout_task in done
    external_cancel = cancel.is_set() and not timed_out

    if not external_cancel:
        cancel.set()
    watcher_task.cancel()
    try:
        await watcher_task
    except BaseException:
        pass

    if timed_out:
        await terminate_all()

    try:
        await asyncio.wait_for(pump_task, timeout=2.0)
    except TimeoutError:
        pump_task.cancel()
        try:
            await pump_task
        except BaseException:
            pass

    try:
        await asyncio.wait_for(asyncio.gather(drain_cat, drain_ssh), timeout=2.0)
    except TimeoutError:
        drain_cat.cancel()
        drain_ssh.cancel()
        await asyncio.gather(drain_cat, drain_ssh, return_exceptions=True)

    duration = time.monotonic() - started
    all_err = [f"[cat] {t}" for t in cat_err_buf] + ssh_err_buf

    if ssh_proc.returncode is None:
        status = "error"
        status_line = "ERROR: ssh process did not exit cleanly"
        exit_code: int | None = None
    elif external_cancel:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (cancelled by client)"
        exit_code = ssh_proc.returncode
    elif timed_out:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (timeout)"
        exit_code = ssh_proc.returncode
    elif ssh_proc.returncode == 0:
        status = "ok"
        status_line = (
            f"OK bytes={bytes_sent} -> {target_host}:{remote_path} duration={duration:.1f}s"
        )
        exit_code = 0
    else:
        status = "failed"
        status_line = (
            f"FAILED bytes={bytes_sent} -> {target_host}:{remote_path} "
            f"exit={ssh_proc.returncode} duration={duration:.1f}s"
        )
        exit_code = ssh_proc.returncode

    log.end(session_id, exit_code, duration, status, lines=len(all_err))

    body = truncate(all_err, 25, 25)
    return f"{status_line}\n\n{body}" if body else status_line


async def get_file(
    *,
    bastion: str,
    target_host: str,
    remote_path: str,
    local_path: str,
    session_id: str,
    timeout_s: float,
    cwd: str,
    user: str | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    """Download ``remote_path`` from ``target_host`` to ``local_path`` via SSH bastion.

    Streams the file contents through SSH stdout (``cat <path>`` on the
    remote end) and writes them to ``local_path`` as bytes arrive.
    Binary-safe. Foreground only — never auto-backgrounds. Hard timeout via
    ``timeout_s``.
    """
    if cancel is None:
        cancel = asyncio.Event()

    local = Path(local_path)
    parent = local.parent
    if not parent.exists():
        return f"ERROR: local parent directory does not exist: {parent}"
    if local.exists() and not local.is_file():
        return f"ERROR: local path exists but is not a regular file: {local_path}"

    target_cmd_str = f"cat {shlex.quote(remote_path)}"
    quoted_target_cmd = shlex.quote(target_cmd_str)

    if target_host == "localhost":
        ssh_argv: list[str] = ["ssh", bastion, quoted_target_cmd]
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        ssh_argv = [
            "ssh", bastion,
            "ssh", shlex.quote(ssh_target), "--", quoted_target_cmd,
        ]

    ssh_proc = await asyncio.create_subprocess_exec(
        *ssh_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ssh_out: asyncio.StreamReader = ssh_proc.stdout  # type: ignore[assignment]
    ssh_err: asyncio.StreamReader = ssh_proc.stderr  # type: ignore[assignment]
    assert ssh_out is not None and ssh_err is not None

    started = time.monotonic()
    log.start(session_id, target_host, f"GET {remote_path} -> {local_path}", cwd)

    try:
        local_file = local.open("wb")
    except OSError as e:
        ssh_proc.kill()
        await ssh_proc.wait()
        return f"ERROR: cannot open local file for writing: {local_path} ({e})"

    bytes_written = 0

    async def pump() -> None:
        nonlocal bytes_written
        try:
            while True:
                chunk = await ssh_out.read(8192)
                if not chunk:
                    break
                local_file.write(chunk)
                bytes_written += len(chunk)
        except Exception:
            pass
        finally:
            try:
                local_file.close()
            except Exception:
                pass

    pump_task = asyncio.create_task(pump())

    ssh_err_buf: list[str] = []

    async def drain_ssh_err() -> None:
        while True:
            raw = await ssh_err.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            ssh_err_buf.append(text)
            log.line(session_id, "stderr", text)

    drain_ssh = asyncio.create_task(drain_ssh_err())

    async def terminate_proc() -> None:
        if ssh_proc.returncode is not None:
            return
        ssh_proc.terminate()
        try:
            await asyncio.wait_for(ssh_proc.wait(), timeout=2.0)
        except TimeoutError:
            ssh_proc.kill()
            await ssh_proc.wait()

    async def cancel_watcher() -> None:
        try:
            await cancel.wait()
        except asyncio.CancelledError:
            return
        await terminate_proc()

    watcher_task = asyncio.create_task(cancel_watcher())
    wait_task = asyncio.create_task(ssh_proc.wait())
    timeout_task: asyncio.Task | None = (
        asyncio.create_task(asyncio.sleep(timeout_s)) if timeout_s > 0 else None
    )

    race_set: list[asyncio.Task] = [wait_task, watcher_task]
    if timeout_task is not None:
        race_set.append(timeout_task)

    done, _ = await asyncio.wait(race_set, return_when=asyncio.FIRST_COMPLETED)

    timed_out = timeout_task is not None and timeout_task in done
    external_cancel = cancel.is_set() and not timed_out

    if not external_cancel:
        cancel.set()
    watcher_task.cancel()
    try:
        await watcher_task
    except BaseException:
        pass

    if timed_out:
        await terminate_proc()

    try:
        await asyncio.wait_for(pump_task, timeout=2.0)
    except TimeoutError:
        pump_task.cancel()
        try:
            await pump_task
        except BaseException:
            pass

    try:
        await asyncio.wait_for(drain_ssh, timeout=2.0)
    except TimeoutError:
        drain_ssh.cancel()
        try:
            await drain_ssh
        except BaseException:
            pass

    duration = time.monotonic() - started

    if ssh_proc.returncode is None:
        status = "error"
        status_line = "ERROR: ssh process did not exit cleanly"
        exit_code: int | None = None
    elif external_cancel:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (cancelled by client)"
        exit_code = ssh_proc.returncode
    elif timed_out:
        status = "cancelled"
        status_line = f"CANCELLED after {duration:.1f}s (timeout)"
        exit_code = ssh_proc.returncode
    elif ssh_proc.returncode == 0:
        status = "ok"
        status_line = (
            f"OK bytes={bytes_written} {target_host}:{remote_path} -> {local_path} "
            f"duration={duration:.1f}s"
        )
        exit_code = 0
    else:
        status = "failed"
        status_line = (
            f"FAILED bytes={bytes_written} {target_host}:{remote_path} -> {local_path} "
            f"exit={ssh_proc.returncode} duration={duration:.1f}s"
        )
        exit_code = ssh_proc.returncode

    log.end(session_id, exit_code, duration, status, lines=len(ssh_err_buf))

    body = truncate(ssh_err_buf, 25, 25)
    return f"{status_line}\n\n{body}" if body else status_line
