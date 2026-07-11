import asyncio
import shlex
import time
from typing import Awaitable, Callable

from . import log
from .truncate import truncate

ProgressFn = Callable[[int, str], Awaitable[None]]


async def run(
    *,
    bastion: str,
    target_host: str,
    command: str,
    session_id: str,
    timeout_s: float,
    progress_interval_s: float,
    progress_tail_lines: int,
    head_lines: int,
    tail_lines: int,
    cwd: str,
    on_progress: ProgressFn | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    if cancel is None:
        cancel = asyncio.Event()

    inner = f"ssh {shlex.quote(target_host)} {shlex.quote(command)}"
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

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        await terminate_proc()

    external_cancel = cancel.is_set() and not timed_out
    if external_cancel:
        try:
            await asyncio.wait_for(watcher_task, timeout=3.0)
        except TimeoutError:
            watcher_task.cancel()
            try:
                await watcher_task
            except BaseException:
                pass
    else:
        cancel.set()
        watcher_task.cancel()
        try:
            await watcher_task
        except BaseException:
            pass

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
