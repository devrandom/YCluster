import asyncio
import json
import os
import re
import shlex
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from dataclasses import dataclass

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
    proc: asyncio.subprocess.Process | None = None
    readers: tuple[asyncio.Task, asyncio.Task] | None = None
    timeout_watcher: asyncio.Task | None = None
    orphaned: bool = False
    finished_at: float | None = None
    # Wall-clock timestamps make the registry snapshot meaningful across a
    # daemon restart (monotonic clocks are process-independent on Linux, but
    # they reset on reboot). `started`/`finished_at` remain monotonic for
    # live timeout arithmetic.
    started_wall: float | None = None
    finished_wall: float | None = None
    finished_reason: str | None = None
    exit_code: int | None = None
    ssh_pid: int | None = None
    # Restored from `CE_STATE_PATH` after a daemon restart. No live process
    # handle or reader tasks survive in the new process.
    recovered: bool = False
    # `principal` ties the session to the client that started it (the MCP
    # session ID from `mcp-session-id`). `list_backgrounded` filters by it
    # so the no-arg `status` listing only shows the caller's own runs.
    # By-ID `status`, `tail`, and `kill` are not scoped — an agent handed a
    # `session_id` can inspect and terminate another thread's run. Only the
    # `/stop` admin endpoint and `kill_all_backgrounded` bypass the kill
    # filter path entirely.
    # `None` is allowed (legacy callers and tests); filtered reads with a
    # `None` principal match only `None`-owned sessions.
    principal: str | None = None


_BACKGROUNDED: dict[str, _Backgrounded] = {}
_STATE_VERSION = 1
_STATE_PATH: Path | None = None
_STATE_LOADED = False
_STATE_LOCK = threading.RLock()
_RECOVERED_POLL_S = 1.0
_LOCAL_SESSION_MARKER = "CE_SESSION"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def init_state(path: Path, *, force: bool = False) -> None:
    """Load the persisted background-session registry from `path`.

    Idempotent per process: the MCP lifespan runs once per client session,
    not once per daemon process, so repeat calls must not clear live entries
    and demote their `asyncio.Process` handles to recovered stubs. Call
    `force=True` only from tests that explicitly simulate a process restart.
    Missing state is normal (first start). A malformed snapshot is reported
    and ignored rather than taking the daemon down; the next mutation
    rewrites it.
    """
    global _STATE_PATH, _STATE_LOADED
    path = Path(path)
    if _STATE_LOADED and not force and path == _STATE_PATH:
        return
    _STATE_PATH = path
    _STATE_LOADED = True
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"cluster-exec: could not read state {path}: {exc}", file=sys.stderr)
        # A transient read failure should be retried by the next lifespan
        # rather than blocking recovery for the rest of the process.
        _STATE_LOADED = False
        return
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"cluster-exec: ignoring malformed state {path}: {exc}", file=sys.stderr)
        return
    if not isinstance(data, dict) or data.get("v") != _STATE_VERSION:
        print(
            f"cluster-exec: ignoring state {path} with unsupported version",
            file=sys.stderr,
        )
        return
    records = data.get("sessions")
    if not isinstance(records, list):
        print(f"cluster-exec: ignoring state {path} without a sessions list", file=sys.stderr)
        return

    restored: dict[str, _Backgrounded] = {}
    now_mono = time.monotonic()
    now_wall = time.time()
    for record in records:
        if not isinstance(record, dict):
            continue
        session_id = record.get("session_id")
        target_host = record.get("target_host")
        command = record.get("command")
        if not (
            isinstance(session_id, str)
            and _SESSION_ID_RE.fullmatch(session_id)
            and isinstance(target_host, str)
            and target_host
            and isinstance(command, str)
            and command
        ):
            continue
        try:
            started_wall = float(record["started_wall"])
            timeout_s = float(record["timeout_s"])
        except (KeyError, TypeError, ValueError):
            continue
        elapsed = max(0.0, now_wall - started_wall)
        finished_wall_raw = record.get("finished_wall")
        finished_wall = (
            float(finished_wall_raw)
            if isinstance(finished_wall_raw, (int, float))
            else None
        )
        finished_at = None
        if finished_wall is not None:
            finished_at = now_mono - max(0.0, now_wall - finished_wall)
        exit_code_raw = record.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
        ssh_pid_raw = record.get("ssh_pid")
        ssh_pid = ssh_pid_raw if isinstance(ssh_pid_raw, int) else None
        principal_raw = record.get("principal")
        principal = principal_raw if isinstance(principal_raw, str) else None
        reason_raw = record.get("finished_reason")
        finished_reason = reason_raw if isinstance(reason_raw, str) else None
        restored[session_id] = _Backgrounded(
            session_id=session_id,
            target_host=target_host,
            command=command,
            started=now_mono - elapsed,
            timeout_s=timeout_s,
            proc=None,
            readers=None,
            timeout_watcher=None,
            orphaned=bool(record.get("orphaned", False)),
            finished_at=finished_at,
            started_wall=started_wall,
            finished_wall=finished_wall,
            finished_reason=finished_reason,
            exit_code=exit_code,
            ssh_pid=ssh_pid,
            recovered=True,
            principal=principal,
        )
    with _STATE_LOCK:
        _BACKGROUNDED.clear()
        _BACKGROUNDED.update(restored)


def _started_wall(bg: _Backgrounded) -> float:
    if bg.started_wall is not None:
        return bg.started_wall
    return time.time() - max(0.0, time.monotonic() - bg.started)


def elapsed_seconds(bg: _Backgrounded) -> float:
    return max(0.0, time.time() - _started_wall(bg))


def returncode(bg: _Backgrounded) -> int | None:
    if bg.proc is not None and bg.proc.returncode is not None:
        return bg.proc.returncode
    return bg.exit_code


def _snapshot_locked() -> list[dict]:
    sessions: list[dict] = []
    for bg in _BACKGROUNDED.values():
        rc = returncode(bg)
        ssh_pid = bg.ssh_pid
        if bg.proc is not None:
            proc_pid = getattr(bg.proc, "pid", None)
            if isinstance(proc_pid, int):
                ssh_pid = proc_pid
        sessions.append(
            {
                "session_id": bg.session_id,
                "target_host": bg.target_host,
                "command": bg.command,
                "started_wall": _started_wall(bg),
                "timeout_s": bg.timeout_s,
                "orphaned": bg.orphaned,
                "finished_wall": bg.finished_wall,
                "finished_reason": bg.finished_reason,
                "exit_code": rc,
                "ssh_pid": ssh_pid,
                "principal": bg.principal,
            }
        )
    return sessions


def _save_state() -> None:
    path = _STATE_PATH
    if path is None:
        return
    with _STATE_LOCK:
        payload = {"v": _STATE_VERSION, "sessions": _snapshot_locked()}
        tmp_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=path.name + ".",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
                f.write("\n")
            os.replace(tmp_name, path)
            tmp_name = None
            os.chmod(path, 0o600)
        except OSError as exc:
            print(f"cluster-exec: could not write state {path}: {exc}", file=sys.stderr)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


def _visible(principal: str | None) -> Callable[["_Backgrounded"], bool]:
    """Build a predicate that gates `_BACKGROUNDED` entries by `principal`.

    `principal=None` means "no filter" (admin / internal use — returns all
    entries). A non-None `principal` matches entries with the same value
    only; entries with `principal=None` are hidden, since they belong to
    legacy callers (no MCP session ID) and should not leak into a
    filtered listing.
    """
    if principal is None:
        return lambda bg: True
    return lambda bg: bg.principal == principal


def list_backgrounded(principal: str | None = None) -> list[str]:
    pred = _visible(principal)
    return [sid for sid, bg in _BACKGROUNDED.items() if pred(bg)]


def get_backgrounded(session_id: str, principal: str | None = None) -> _Backgrounded | None:
    bg = _BACKGROUNDED.get(session_id)
    if bg is None:
        return None
    if not _visible(principal)(bg):
        return None
    return bg


def kill_backgrounded(
    session_id: str, grace_s: float = 2.0, principal: str | None = None
) -> bool:
    bg = _BACKGROUNDED.get(session_id)
    if bg is None or not _visible(principal)(bg):
        return False
    return _kill_unconditionally(session_id, grace_s)


def _kill_unconditionally(
    session_id: str, grace_s: float = 2.0, retain: bool = False
) -> bool:
    """Terminate a session without checking `principal`.

    Used by `kill_all_backgrounded` (admin) and after we've already
    verified the principal in `kill_backgrounded`. `retain=True` keeps a
    terminal record (and therefore the persisted status) while still
    terminating the process; daemon shutdown uses this so a restart does
    not erase session history.
    """
    bg = _BACKGROUNDED.get(session_id)
    if bg is None:
        return False
    rc = returncode(bg)
    was_finished = bg.finished_at is not None or rc is not None
    if bg.timeout_watcher is not None and not bg.timeout_watcher.done():
        bg.timeout_watcher.cancel()
        try:
            await_cancelled(bg.timeout_watcher)
        except Exception:
            pass
    if bg.proc is not None:
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
    elif not was_finished:
        # Recovered after a daemon restart: the old asyncio Process handle
        # is gone, but the local ssh child is still identifiable by the
        # CE_SESSION marker in its argv. Do not discard the status record
        # unless the child is confirmed gone or terminated.
        pids = _local_session_pids(session_id)
        if not pids:
            _mark_backgrounded_finished(
                session_id,
                reason="process-gone-after-daemon-restart",
            )
            return False
        if not _terminate_local_session(session_id, grace_s=grace_s):
            return False
    if retain:
        _mark_backgrounded_finished(
            session_id,
            exit_code=returncode(bg),
            reason=None if was_finished else "stopped-with-daemon",
        )
    else:
        _BACKGROUNDED.pop(session_id, None)
        _save_state()
    return True


def kill_all_backgrounded(grace_s: float = 2.0, retain: bool = False) -> int:
    # Snapshot keys: the default path mutates the dict as it pops.
    # Admin endpoint — bypasses principal filtering on purpose.
    count = 0
    for sid in list(_BACKGROUNDED.keys()):
        bg = _BACKGROUNDED.get(sid)
        was_live = (
            bg is not None
            and bg.finished_at is None
            and returncode(bg) is None
        )
        if _kill_unconditionally(sid, grace_s=grace_s, retain=retain):
            if not retain or was_live:
                count += 1
    return count


def _local_session_pids(session_id: str) -> list[int]:
    """Return local PIDs whose argv carries this session's marker."""
    needle = f"{_LOCAL_SESSION_MARKER}={session_id}".encode()
    pids: list[int] = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return pids
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                cmdline = Path("/proc", entry.name, "cmdline").read_bytes()
            except OSError:
                continue
            if needle in cmdline:
                pids.append(int(entry.name))
    return pids


def _wait_for_local_session_exit(session_id: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if not _local_session_pids(session_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate_local_session(session_id: str, grace_s: float = 2.0) -> bool:
    """SIGTERM, wait, then SIGKILL a recovered local ssh child.

    Returns True only when no process carrying the session marker remains.
    """
    pids = _local_session_pids(session_id)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            print(
                f"cluster-exec: could not SIGTERM recovered session {session_id} pid {pid}: {exc}",
                file=sys.stderr,
            )
    if _wait_for_local_session_exit(session_id, grace_s):
        return True
    for pid in _local_session_pids(session_id):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            print(
                f"cluster-exec: could not SIGKILL recovered session {session_id} pid {pid}: {exc}",
                file=sys.stderr,
            )
    return _wait_for_local_session_exit(session_id, 1.0)


async def _find_local_session_pid(session_id: str) -> int | None:
    pattern = f"{_LOCAL_SESSION_MARKER}={session_id}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep",
            "-f",
            pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"cluster-exec: could not inspect recovered session {session_id}: {exc}", file=sys.stderr)
        return None
    stdout, _ = await proc.communicate()
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            return int(raw.strip())
        except ValueError:
            continue
    return None


async def _recovered_watcher(session_id: str) -> None:
    """Track a session restored from disk until it exits or hits its timeout."""
    try:
        while True:
            bg = _BACKGROUNDED.get(session_id)
            if bg is None or bg.finished_at is not None:
                return
            pid = await _find_local_session_pid(session_id)
            bg = _BACKGROUNDED.get(session_id)
            if bg is None or bg.finished_at is not None:
                return
            if pid is None:
                _mark_backgrounded_finished(
                    session_id,
                    reason="process-gone-after-daemon-restart",
                )
                return
            if bg.ssh_pid != pid:
                bg.ssh_pid = pid
                _save_state()
            elapsed = elapsed_seconds(bg)
            remaining = (
                max(0.0, bg.timeout_s - elapsed) if bg.timeout_s > 0 else None
            )
            if remaining is not None and remaining <= 0:
                if not _local_session_pids(session_id):
                    _mark_backgrounded_finished(
                        session_id,
                        reason="process-gone-after-daemon-restart",
                    )
                    return
                terminated = await asyncio.to_thread(
                    _terminate_local_session,
                    session_id,
                )
                if terminated:
                    _mark_backgrounded_finished(
                        session_id,
                        reason="hard-timeout-after-daemon-restart",
                    )
                    return
                # Termination was not confirmed. Keep the record and retry
                # on the next poll rather than declaring a live process dead.
            sleep_s = _RECOVERED_POLL_S
            if remaining is not None:
                sleep_s = min(sleep_s, remaining)
            await asyncio.sleep(max(0.05, sleep_s))
    except asyncio.CancelledError:
        return


async def start_recovered_watchers() -> None:
    """Start poll loops for sessions restored from the state file.

    Idempotent; called from the MCP lifespan and from `status` so a direct
    tool invocation in tests or an embedded server still gets recovery.
    """
    for bg in list(_BACKGROUNDED.values()):
        if not bg.recovered or bg.finished_at is not None:
            continue
        if bg.timeout_watcher is not None and not bg.timeout_watcher.done():
            continue
        bg.timeout_watcher = asyncio.create_task(_recovered_watcher(bg.session_id))


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
    principal: str | None = None,
) -> None:
    proc_pid = getattr(proc, "pid", None)
    ssh_pid = proc_pid if isinstance(proc_pid, int) else None
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
        started_wall=time.time() - max(0.0, time.monotonic() - started),
        ssh_pid=ssh_pid,
        principal=principal,
    )
    _save_state()


def _mark_backgrounded_finished(
    session_id: str,
    *,
    exit_code: int | None = None,
    reason: str | None = None,
) -> None:
    bg = _BACKGROUNDED.get(session_id)
    if bg is None:
        return
    if bg.finished_at is None:
        bg.finished_at = time.monotonic()
        bg.finished_wall = time.time()
    if exit_code is not None:
        bg.exit_code = exit_code
    if reason is not None and bg.finished_reason is None:
        bg.finished_reason = reason
    _save_state()


def _ssh_mux_args(control_path: str | None, control_persist_s: float) -> list[str]:
    """Return ssh options for a reusable ControlMaster connection.

    Multiplexing is opt-in through a non-empty path and a positive persist
    time. The socket path must be short enough for ``sockaddr_un`` (OpenSSH
    expands ``%C`` to a 40-character hash); callers normally use
    ``~/.ssh/cluster-exec-%C`` so different bastions get different sockets.
    """
    if not control_path or control_persist_s <= 0:
        return []
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_path}",
        "-o",
        f"ControlPersist={max(1, int(control_persist_s))}",
    ]


def _run_ssh_argv(
    bastion: str,
    session_id: str,
    remote_command: str,
    *,
    control_path: str | None = None,
    control_persist_s: float = 0.0,
) -> list[str]:
    # The marker is local argv only (ssh's SetEnv option), so pgrep/pkill can
    # find a surviving child after the daemon that spawned it has exited.
    # It does not alter the command sent to the target.
    return [
        "ssh",
        "-o",
        f"SetEnv={_LOCAL_SESSION_MARKER}={session_id}",
        *_ssh_mux_args(control_path, control_persist_s),
        bastion,
        remote_command,
    ]


async def close_ssh_master(
    *,
    bastion: str | None,
    control_path: str | None,
    control_persist_s: float,
    timeout_s: float = 5.0,
) -> bool:
    """Ask the persistent ControlMaster for ``bastion`` to exit.

    The master is deliberately not tied to any one tool call: it is what
    makes the next call fast. Daemon shutdown should close it so a stopped
    cluster-exec does not leave a bastion connection alive for the rest of
    ``ControlPersist``. A missing socket is normal and returns False.
    """
    if not bastion or not control_path or control_persist_s <= 0:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            f"ControlPath={control_path}",
            "-O",
            "exit",
            bastion,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"cluster-exec: could not close ssh control socket: {exc}", file=sys.stderr)
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0


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
    principal: str | None = None,
    control_path: str | None = None,
    control_persist_s: float = 0.0,
) -> str:
    if cancel is None:
        cancel = asyncio.Event()
    if auto_bg_after_s > 0 and timeout_s > 0 and timeout_s <= auto_bg_after_s:
        raise ValueError(
            f"timeout_s ({timeout_s}) must be greater than auto_bg_after_s ({auto_bg_after_s})"
        )

    if target_host == "localhost":
        ssh_argv = _run_ssh_argv(
            bastion,
            session_id,
            command,
            control_path=control_path,
            control_persist_s=control_persist_s,
        )
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        inner = f"ssh {shlex.quote(ssh_target)} {shlex.quote(command)}"
        ssh_argv = _run_ssh_argv(
            bastion,
            session_id,
            inner,
            control_path=control_path,
            control_persist_s=control_persist_s,
        )
    proc = await asyncio.create_subprocess_exec(
        *ssh_argv,
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

    async def finalize_backgrounded(
        *, exit_code: int | None = None, reason: str | None = None
    ) -> None:
        # Keep the session in the registry so `status` can still show its
        # terminal state; `kill` removes it explicitly, while daemon
        # shutdown retains a terminal record for the next process.
        _mark_backgrounded_finished(
            session_id,
            exit_code=exit_code,
            reason=reason,
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
                await finalize_backgrounded(exit_code=proc.returncode)
                return
        # Hard timeout fired (or remaining <= 0). Proc is still alive
        # here (else wait_task would have been in done).
        if proc.returncode is None:
            await terminate_proc()
        await finalize_backgrounded(
            exit_code=proc.returncode,
            reason="hard-timeout",
        )

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
                principal=principal,
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
            principal=principal,
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
    control_path: str | None = None,
    control_persist_s: float = 0.0,
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

    mux_args = _ssh_mux_args(control_path, control_persist_s)
    if target_host == "localhost":
        ssh_argv: list[str] = ["ssh", *mux_args, bastion, quoted_target_cmd]
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        ssh_argv = [
            "ssh", *mux_args, bastion,
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
    cat_out = cat_proc.stdout
    ssh_in = ssh_proc.stdin
    cat_err = cat_proc.stderr
    ssh_err = ssh_proc.stderr
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
    control_path: str | None = None,
    control_persist_s: float = 0.0,
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

    mux_args = _ssh_mux_args(control_path, control_persist_s)
    if target_host == "localhost":
        # Do not shlex-quote the whole command here: ssh joins its argv and
        # the bastion shell re-parses the result. Quoting the entire
        # ``cat <path>`` string would make the shell look for a program whose
        # name contains a space. The path itself is already quoted above.
        ssh_argv: list[str] = ["ssh", *mux_args, bastion, target_cmd_str]
    else:
        ssh_target = f"{user}@{target_host}" if user else target_host
        ssh_argv = [
            "ssh", *mux_args, bastion,
            "ssh", shlex.quote(ssh_target), "--", shlex.quote(target_cmd_str),
        ]

    ssh_proc = await asyncio.create_subprocess_exec(
        *ssh_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ssh_out = ssh_proc.stdout
    ssh_err = ssh_proc.stderr
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
