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


def _patched_run(proc: _FakeProcess, *, timeout_s: float, auto_bg_after_s: float = 0.0, progress_interval_s: float = 10.0, target_host: str = "dummy-target", **kwargs):
    async def _create(*a, **kw):
        return proc
    with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
        return asyncio.run(
            exec_mod.run(
                bastion="dummy-bastion",
                target_host=target_host,
                command="dummy-cmd",
                session_id="abcd1234",
                timeout_s=timeout_s,
                auto_bg_after_s=auto_bg_after_s,
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

    def test_localhost_skips_inner_ssh(self):
        proc = _FakeProcess(stdout_lines=[b"ok\n"], stderr_lines=[], exit_code=0)
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return proc

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            asyncio.run(
                exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="localhost",
                    command="dummy-cmd",
                    session_id="abcd1234",
                    timeout_s=10,
                    auto_bg_after_s=0.0,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                )
            )
        self.assertEqual(len(captured), 1)
        args = captured[0]
        self.assertEqual(args[0], "ssh")
        self.assertEqual(args[1], "dummy-bastion")
        self.assertEqual(args[2], "dummy-cmd")
        self.assertNotIn("ssh localhost", args[2])

    def test_remote_target_wraps_inner_ssh(self):
        proc = _FakeProcess(stdout_lines=[b"ok\n"], stderr_lines=[], exit_code=0)
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return proc

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            asyncio.run(
                exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="app1",
                    command="echo hello world",
                    session_id="abcd1234",
                    timeout_s=10,
                    auto_bg_after_s=0.0,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                )
            )
        self.assertEqual(len(captured), 1)
        args = captured[0]
        self.assertEqual(args[0], "ssh")
        self.assertEqual(args[1], "dummy-bastion")
        self.assertEqual(args[2], "ssh app1 'echo hello world'")

    def test_remote_target_with_user_prefixes_inner_ssh(self):
        """`user="admin"` produces `ssh admin@app1 ...` on the inner hop.

        Needed for macOS nodes (and similar) where the bastion's ssh_config
        default User is `root` but root login is denied — the caller can
        override per-call without editing the config.
        """
        proc = _FakeProcess(stdout_lines=[b"ok\n"], stderr_lines=[], exit_code=0)
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return proc

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            asyncio.run(
                exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="app1",
                    command="echo hello world",
                    session_id="abcd1234",
                    timeout_s=10,
                    auto_bg_after_s=0.0,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                    user="admin",
                )
            )
        self.assertEqual(len(captured), 1)
        args = captured[0]
        self.assertEqual(args[0], "ssh")
        self.assertEqual(args[1], "dummy-bastion")
        self.assertEqual(args[2], "ssh admin@app1 'echo hello world'")

    def test_localhost_skips_inner_ssh_with_user(self):
        """`user` is a no-op for `hostname="localhost"` — the command runs
        directly on the bastion, so there's no inner ssh to prefix."""
        proc = _FakeProcess(stdout_lines=[b"ok\n"], stderr_lines=[], exit_code=0)
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return proc

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            asyncio.run(
                exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="localhost",
                    command="dummy-cmd",
                    session_id="abcd1234",
                    timeout_s=10,
                    auto_bg_after_s=0.0,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                    user="admin",
                )
            )
        self.assertEqual(len(captured), 1)
        args = captured[0]
        self.assertEqual(args[0], "ssh")
        self.assertEqual(args[1], "dummy-bastion")
        self.assertEqual(args[2], "dummy-cmd")

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
                auto_bg_after_s=0.0,
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

    def test_auto_bg_fires_before_proc_exits(self):
        proc = _FakeProcess(
            stdout_lines=[b"first\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=10.0,
        )
        text = _patched_run(proc, timeout_s=30, auto_bg_after_s=0.2)
        self.assertTrue(text.startswith("BACKGROUNDED"))
        self.assertIn("session=abcd1234", text)
        self.assertIn("tail", text)
        self.assertIn("first", text)
        self.assertEqual(proc._returncode, None)
        self.assertFalse(proc._terminated)
        self.assertFalse(proc._killed)
        self.assertIn("abcd1234", exec_mod.list_backgrounded())
        try:
            exec_mod.kill_backgrounded("abcd1234")
        finally:
            exec_mod._BACKGROUNDED.clear()

    def test_bg_proc_outlives_exec_run(self):
        proc = _FakeProcess(
            stdout_lines=[],
            stderr_lines=[],
            exit_code=0,
            wait_delay=10.0,
        )

        async def runner():
            async def _create(*a, **kw):
                return proc
            with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
                return await exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="dummy-target",
                    command="dummy-cmd",
                    session_id="outlive01",
                    timeout_s=30,
                    auto_bg_after_s=0.1,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                )

        text = asyncio.run(runner())
        self.assertTrue(text.startswith("BACKGROUNDED"))
        self.assertEqual(proc._returncode, None)
        self.assertFalse(proc._terminated)
        self.assertFalse(proc._killed)
        self.assertIn("outlive01", exec_mod.list_backgrounded())
        try:
            exec_mod.kill_backgrounded("outlive01")
        finally:
            exec_mod._BACKGROUNDED.clear()

    def test_hard_timeout_kills_bg_session(self):
        proc = _FakeProcess(
            stdout_lines=[b"alive\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=10.0,
        )

        async def runner():
            async def _create(*a, **kw):
                return proc
            with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
                text = await exec_mod.run(
                    bastion="dummy-bastion",
                    target_host="dummy-target",
                    command="dummy-cmd",
                    session_id="hards01",
                    timeout_s=0.3,
                    auto_bg_after_s=0.1,
                    progress_interval_s=10.0,
                    progress_tail_lines=50,
                    head_lines=100,
                    tail_lines=100,
                    cwd="/cwd",
                )
            await asyncio.sleep(0.4)
            return text, proc._returncode

        text, rc = asyncio.run(runner())
        self.assertTrue(text.startswith("BACKGROUNDED"))
        self.assertIn("alive", text)
        self.assertIsNotNone(rc)
        self.assertTrue(proc._terminated or proc._killed)
        try:
            exec_mod.kill_backgrounded("hards01")
        finally:
            exec_mod._BACKGROUNDED.clear()

    def test_invalid_timeout_ordering_raises(self):
        proc = _FakeProcess(
            stdout_lines=[],
            stderr_lines=[],
            exit_code=0,
        )
        async def _create(*a, **kw):
            return proc
        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            with self.assertRaises(ValueError):
                asyncio.run(
                    exec_mod.run(
                        bastion="dummy-bastion",
                        target_host="dummy-target",
                        command="dummy-cmd",
                        session_id="badcfg01",
                        timeout_s=10.0,
                        auto_bg_after_s=20.0,
                        progress_interval_s=10.0,
                        progress_tail_lines=50,
                        head_lines=100,
                        tail_lines=100,
                        cwd="/cwd",
                    )
                )

    def test_auto_bg_disabled_when_zero(self):
        proc = _FakeProcess(
            stdout_lines=[b"hi\n"],
            stderr_lines=[],
            exit_code=0,
            wait_delay=0.1,
        )
        text = _patched_run(proc, timeout_s=10, auto_bg_after_s=0)
        self.assertTrue(text.startswith("OK"))
        self.assertIn("hi", text)
        self.assertEqual(exec_mod.list_backgrounded(), [])


if __name__ == "__main__":
    unittest.main()
