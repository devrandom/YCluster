import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cluster_exec import exec as exec_mod
from cluster_exec import log


class _FakeStream:
    def __init__(self, lines: list[bytes]):
        self._iter = iter(lines + [b""])

    async def readline(self) -> bytes:
        return next(self._iter, b"")


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_lines: list[bytes],
        exit_code: int,
        wait_delay: float = 0.0,
    ) -> None:
        self._stdout = _FakeStream(stdout_lines)
        self._stderr = _FakeStream(stderr_lines)
        self._exit_code = exit_code
        self._wait_delay = wait_delay
        self._terminated = False
        self._killed = False
        self._returncode: int | None = None

    @property
    def stdout(self) -> _FakeStream:
        return self._stdout

    @property
    def stderr(self) -> _FakeStream:
        return self._stderr

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        if self._wait_delay and not self._terminated and not self._killed:
            await asyncio.sleep(self._wait_delay)
        self._returncode = self._exit_code
        return self._exit_code

    def terminate(self) -> None:
        self._terminated = True
        if self._returncode is None:
            self._returncode = -15  # SIGTERM

    def kill(self) -> None:
        self._killed = True
        if self._returncode is None:
            self._returncode = -9  # SIGKILL


def _patched_run(proc: _FakeProcess, *, timeout_s: float, progress_interval_s: float = 10.0, **kwargs):
    async def _create(*a, **kw):
        return proc
    with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
        return asyncio.run(
            exec_mod.run(
                bastion="dummy-bastion",
                target_host="dummy-target",
                command="dummy-cmd",
                session_id="abcd1234",
                timeout_s=timeout_s,
                progress_interval_s=progress_interval_s,
                progress_tail_lines=50,
                head_lines=kwargs.get("head_lines", 100),
                tail_lines=kwargs.get("tail_lines", 100),
                cwd="/cwd",
                on_progress=kwargs.get("on_progress"),
                cancel=kwargs.get("cancel"),
            )
        )


class ExecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_exit_zero(self):
        proc = _FakeProcess(
            stdout_lines=[b"hello\n", b"world\n"],
            stderr_lines=[],
            exit_code=0,
        )
        text = _patched_run(proc, timeout_s=10)
        self.assertTrue(text.startswith("OK exit=0"))
        self.assertIn("hello", text)
        self.assertIn("world", text)

    def test_nonzero_exit(self):
        proc = _FakeProcess(
            stdout_lines=[b"err msg\n"],
            stderr_lines=[],
            exit_code=2,
        )
        text = _patched_run(proc, timeout_s=10)
        self.assertTrue(text.startswith("FAILED exit=2"))
        self.assertIn("err msg", text)

    def test_stderr_tagged(self):
        proc = _FakeProcess(
            stdout_lines=[b"ok\n"],
            stderr_lines=[b"warning\n"],
            exit_code=0,
        )
        text = _patched_run(proc, timeout_s=10)
        self.assertIn("[stderr] warning", text)
        self.assertIn("ok", text)

    def test_timeout_kills(self):
        proc = _FakeProcess(
            stdout_lines=[b"slow\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=10.0,
        )
        text = _patched_run(proc, timeout_s=0.2)
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("slow", text)
        self.assertTrue(proc._terminated or proc._killed)

    def test_truncation(self):
        lines = [f"line{i}\n".encode() for i in range(200)]
        proc = _FakeProcess(stdout_lines=lines, stderr_lines=[], exit_code=0)
        text = _patched_run(proc, timeout_s=10, head_lines=3, tail_lines=3)
        self.assertIn("line0", text)
        self.assertIn("line199", text)
        self.assertIn("… [194 lines elided] …", text)

    def test_progress_callback_invoked(self):
        proc = _FakeProcess(
            stdout_lines=[b"a\n", b"b\n", b"c\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=2.0,
        )
        calls: list[tuple[int, str]] = []

        async def cb(progress: int, message: str) -> None:
            calls.append((progress, message))

        text = _patched_run(
            proc,
            timeout_s=10,
            progress_interval_s=0.2,
            on_progress=cb,
        )
        self.assertTrue(text.startswith("OK"))
        self.assertGreater(len(calls), 0)
        for _progress, message in calls:
            self.assertIsInstance(message, str)

    def test_external_cancel_terminates_proc(self):
        proc = _FakeProcess(
            stdout_lines=[b"started\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=5.0,
        )
        cancel = asyncio.Event()

        async def runner():
            async def fire():
                await asyncio.sleep(0.2)
                cancel.set()

            fire_task = asyncio.create_task(fire())
            text = await exec_mod.run(
                bastion="dummy-bastion",
                target_host="dummy-target",
                command="dummy-cmd",
                session_id="abcd1234",
                timeout_s=10,
                progress_interval_s=10.0,
                progress_tail_lines=50,
                head_lines=100,
                tail_lines=100,
                cwd="/cwd",
                cancel=cancel,
            )
            fire_task.cancel()
            return text

        async def _create(*a, **kw):
            return proc

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            text = asyncio.run(runner())

        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("cancelled by client", text)
        self.assertTrue(proc._terminated or proc._killed)

    def test_external_cancel_distinct_from_timeout(self):
        proc = _FakeProcess(
            stdout_lines=[],
            stderr_lines=[],
            exit_code=0,
            wait_delay=5.0,
        )
        cancel = asyncio.Event()
        cancel.set()

        text = _patched_run(
            proc,
            timeout_s=10,
            progress_interval_s=10.0,
            cancel=cancel,
        )
        self.assertIn("cancelled by client", text)
        self.assertNotIn("timeout", text)

    def test_timeout_path_distinct_from_external_cancel(self):
        proc = _FakeProcess(
            stdout_lines=[],
            stderr_lines=[],
            exit_code=0,
            wait_delay=10.0,
        )
        text = _patched_run(proc, timeout_s=0.2)
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("timeout", text)
        self.assertNotIn("cancelled by client", text)


if __name__ == "__main__":
    unittest.main()
