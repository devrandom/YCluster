import asyncio
import shlex
import time
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

    race_set: list[asyncio.Task] = [wait_task]
    if bg_task is not None:
        race_set.append(bg_task)
    if timeout_task is not None:
        race_set.append(timeout_task)

    done, _ = await asyncio.wait(race_set, return_when=asyncio.FIRST_COMPLETED)

    backgrounded = bg_task is not None and bg_task in done
    timed_out = timeout_task is not None and timeout_task in done

    if backgrounded:

        async def timeout_watcher_fn() -> None:
            elapsed = time.monotonic() - started
            remaining = timeout_s - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
            if proc.returncode is None:
                await terminate_proc()

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
