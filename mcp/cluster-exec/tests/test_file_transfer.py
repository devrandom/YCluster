import asyncio
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cluster_exec import exec as exec_mod
from cluster_exec import log


class _FakeLineStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._iter = iter(lines + [b""])

    async def readline(self) -> bytes:
        return next(self._iter, b"")


class _FakeByteStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = min(self._pos + (n if n > 0 else len(self._data)), len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class _FakeStdin:
    def __init__(self) -> None:
        self.received: bytearray = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("write to closed stdin")
        self.received.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True

    def is_closing(self) -> bool:
        return self._closed


class _FakeProc:
    def __init__(
        self,
        *,
        stdout_data: bytes = b"",
        stderr_lines: list[bytes] | None = None,
        exit_code: int = 0,
        wait_delay: float = 0.0,
    ) -> None:
        self._stdout = _FakeByteStream(stdout_data)
        self._stderr = _FakeLineStream(stderr_lines or [])
        self._stdin = _FakeStdin()
        self._exit_code = exit_code
        self._wait_delay = wait_delay
        self._terminated = False
        self._killed = False
        self._returncode: int | None = None

    @property
    def stdout(self) -> _FakeByteStream:
        return self._stdout

    @property
    def stderr(self) -> _FakeLineStream:
        return self._stderr

    @property
    def stdin(self) -> _FakeStdin:
        return self._stdin

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
            self._returncode = -15

    def kill(self) -> None:
        self._killed = True
        if self._returncode is None:
            self._returncode = -9


class PutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_local(self, name: str = "src.bin", content: bytes = b"") -> Path:
        p = Path(self.tmp.name) / name
        p.write_bytes(content)
        return p

    def _run_with(
        self,
        procs: list[_FakeProc],
        **kwargs,
    ) -> tuple[str, list[tuple], list[dict]]:
        argv_lists: list[tuple] = []
        kwarg_lists: list[dict] = []
        proc_iter = iter(procs)

        async def _create(*args, **kw):
            argv_lists.append(args)
            kwarg_lists.append(kw)
            try:
                return next(proc_iter)
            except StopIteration:
                raise AssertionError(f"create_subprocess_exec called too many times: {args}")

        bastion: str = kwargs.get("bastion", "dummy-bastion")
        target_host: str = kwargs.get("target_host", "app1")
        local_path: str = kwargs["local_path"]
        remote_path: str = kwargs.get("remote_path", "/dummy/remote")
        session_id: str = kwargs.get("session_id", "put12345")
        timeout_s: float = kwargs.get("timeout_s", 10.0)
        cwd: str = kwargs.get("cwd", "/cwd")
        cancel: asyncio.Event | None = kwargs.get("cancel", None)

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            text = asyncio.run(
                exec_mod.put_file(
                    bastion=bastion,
                    target_host=target_host,
                    local_path=local_path,
                    remote_path=remote_path,
                    session_id=session_id,
                    timeout_s=timeout_s,
                    cwd=cwd,
                    cancel=cancel,
                )
            )
        return text, argv_lists, kwarg_lists

    def test_localhost_skips_inner_ssh(self):
        local = self._make_local(content=b"x")
        cat = _FakeProc(stdout_data=b"x")
        ssh = _FakeProc(exit_code=0)
        text, argv_lists, _ = self._run_with(
            procs=[cat, ssh],
            target_host="localhost",
            local_path=str(local),
            remote_path="/tmp/dst",
        )
        self.assertTrue(text.startswith("OK bytes=1"))
        self.assertEqual(len(argv_lists), 2)
        cat_argv = argv_lists[0]
        ssh_argv = argv_lists[1]
        self.assertEqual(cat_argv[0], "cat")
        self.assertEqual(ssh_argv[0], "ssh")
        self.assertEqual(ssh_argv[1], "dummy-bastion")
        self.assertNotIn("ssh localhost", ssh_argv[2])

    def test_remote_target_creates_two_subprocesses(self):
        local = self._make_local(content=b"x")
        cat = _FakeProc(stdout_data=b"x")
        ssh = _FakeProc(exit_code=0)
        text, argv_lists, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst",
        )
        self.assertTrue(text.startswith("OK bytes=1"))
        self.assertEqual(len(argv_lists), 2)
        ssh_argv = argv_lists[1]
        self.assertEqual(ssh_argv[0], "ssh")
        self.assertEqual(ssh_argv[1], "dummy-bastion")
        self.assertEqual(ssh_argv[2], "ssh")
        self.assertEqual(ssh_argv[3], shlex.quote("app1"))
        self.assertEqual(ssh_argv[4], "--")

    def test_ssh_subprocess_uses_stdin_pipe_not_streamreader(self):
        """Regression test: passing cat_proc.stdout (a StreamReader) as stdin
        to create_subprocess_exec doesn't work — asyncio tries to call
        fileno() on it and StreamReader has none. The fix uses stdin=PIPE
        and a pump coroutine."""
        local = self._make_local(content=b"x")
        cat = _FakeProc(stdout_data=b"x")
        ssh = _FakeProc(exit_code=0)
        text, argv_lists, kwarg_lists = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst",
        )
        self.assertTrue(text.startswith("OK bytes=1"))
        ssh_kwargs = kwarg_lists[1]
        self.assertEqual(
            ssh_kwargs.get("stdin"),
            asyncio.subprocess.PIPE,
            "ssh subprocess must use stdin=PIPE; passing a StreamReader "
            "breaks asyncio.create_subprocess_exec",
        )
        self.assertEqual(ssh_kwargs.get("stdout"), asyncio.subprocess.PIPE)
        self.assertEqual(ssh_kwargs.get("stderr"), asyncio.subprocess.PIPE)

    def test_local_file_not_found_returns_error(self):
        text, argv_lists, _ = self._run_with(
            procs=[],
            local_path="/no/such/file/xyz",
            remote_path="/tmp/dst",
        )
        self.assertIn("ERROR: local file not found", text)
        self.assertEqual(argv_lists, [])

    def test_local_path_is_directory_returns_error(self):
        text, argv_lists, _ = self._run_with(
            procs=[],
            local_path=self.tmp.name,
            remote_path="/tmp/dst",
        )
        self.assertIn("ERROR: local path is not a regular file", text)
        self.assertEqual(argv_lists, [])

    def test_happy_path_pumps_all_bytes_to_ssh_stdin(self):
        payload = b"hello world" * 100
        local = self._make_local(content=payload)
        cat = _FakeProc(stdout_data=payload)
        ssh = _FakeProc(exit_code=0)
        text, _, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst.bin",
        )
        self.assertTrue(text.startswith(f"OK bytes={len(payload)}"))
        self.assertIn("app1:/tmp/dst.bin", text)
        # The pump must have transferred every byte to ssh.stdin, and
        # closed the pipe so ssh sees EOF.
        self.assertEqual(bytes(ssh.stdin.received), payload)
        self.assertTrue(ssh.stdin.is_closing())

    def test_happy_path_reports_byte_count(self):
        # Backward-compatible assertion: status line byte count matches
        # what was actually sent to ssh.stdin.
        payload = b"hello world" * 100
        local = self._make_local(content=payload)
        cat = _FakeProc(stdout_data=payload)
        ssh = _FakeProc(exit_code=0)
        text, _, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst.bin",
        )
        self.assertTrue(text.startswith(f"OK bytes={len(payload)}"))

    def test_pump_closes_ssh_stdin_on_nonzero_exit(self):
        # If ssh exits nonzero, the pump should still close stdin cleanly
        # (so the cat process doesn't hang trying to write).
        local = self._make_local(content=b"data")
        cat = _FakeProc(stdout_data=b"data")
        ssh = _FakeProc(exit_code=1, stderr_lines=[b"ssh: permission denied\n"])
        text, _, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst.bin",
        )
        self.assertTrue(text.startswith("FAILED"))
        self.assertIn("exit=1", text)
        self.assertIn("ssh: permission denied", text)
        self.assertTrue(ssh.stdin.is_closing())

    def test_nonzero_exit_reports_failure(self):
        local = self._make_local(content=b"data")
        cat = _FakeProc(stdout_data=b"data")
        ssh = _FakeProc(exit_code=1, stderr_lines=[b"ssh: permission denied\n"])
        text, _, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst.bin",
        )
        self.assertTrue(text.startswith("FAILED"))
        self.assertIn("exit=1", text)
        self.assertIn("ssh: permission denied", text)

    def test_timeout_kills(self):
        local = self._make_local(content=b"data")
        cat = _FakeProc(stdout_data=b"data", wait_delay=10.0)
        ssh = _FakeProc(exit_code=0, wait_delay=10.0)
        text, _, _ = self._run_with(
            procs=[cat, ssh],
            local_path=str(local),
            remote_path="/tmp/dst.bin",
            timeout_s=0.2,
        )
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("timeout", text)
        self.assertTrue(ssh._terminated or ssh._killed)
        self.assertTrue(cat._terminated or cat._killed)

    def test_external_cancel_terminates(self):
        local = self._make_local(content=b"data")
        cat = _FakeProc(stdout_data=b"data", wait_delay=10.0)
        ssh = _FakeProc(exit_code=0, wait_delay=10.0)
        cancel = asyncio.Event()

        async def runner():
            async def fire():
                await asyncio.sleep(0.2)
                cancel.set()

            fire_task = asyncio.create_task(fire())
            proc_iter = iter([cat, ssh])

            async def _create(*args, **kw):
                return next(proc_iter)

            with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
                text = await exec_mod.put_file(
                    bastion="dummy-bastion",
                    target_host="app1",
                    local_path=str(local),
                    remote_path="/tmp/dst.bin",
                    session_id="put12345",
                    timeout_s=10,
                    cwd="/cwd",
                    cancel=cancel,
                )
            fire_task.cancel()
            try:
                await fire_task
            except BaseException:
                pass
            return text

        text = asyncio.run(runner())
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("cancelled by client", text)
        self.assertTrue(ssh._terminated or ssh._killed)
        self.assertTrue(cat._terminated or cat._killed)


class GetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_with(self, proc: _FakeProc, **kwargs) -> tuple[str, list[tuple], bytes]:
        argv_lists: list[tuple] = []

        async def _create(*args, **kw):
            argv_lists.append(args)
            return proc

        with tempfile.TemporaryDirectory() as outdir:
            local = Path(outdir) / "out.bin"
            bastion: str = kwargs.get("bastion", "dummy-bastion")
            target_host: str = kwargs.get("target_host", "app1")
            remote_path: str = kwargs.get("remote_path", "/etc/hostname")
            session_id: str = kwargs.get("session_id", "get12345")
            timeout_s: float = kwargs.get("timeout_s", 10.0)
            cwd: str = kwargs.get("cwd", "/cwd")
            cancel: asyncio.Event | None = kwargs.get("cancel", None)
            with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
                text = asyncio.run(
                    exec_mod.get_file(
                        bastion=bastion,
                        target_host=target_host,
                        remote_path=remote_path,
                        local_path=str(local),
                        session_id=session_id,
                        timeout_s=timeout_s,
                        cwd=cwd,
                        cancel=cancel,
                    )
                )
            content = local.read_bytes() if local.exists() else b""
        return text, argv_lists, content

    def test_localhost_skips_inner_ssh(self):
        ssh = _FakeProc(stdout_data=b"hello")
        text, argv_lists, content = self._run_with(ssh, target_host="localhost")
        self.assertTrue(text.startswith("OK bytes=5"))
        self.assertEqual(content, b"hello")
        self.assertEqual(len(argv_lists), 1)
        self.assertEqual(argv_lists[0][0], "ssh")
        self.assertEqual(argv_lists[0][1], "dummy-bastion")
        self.assertNotIn("ssh localhost", argv_lists[0][2])

    def test_remote_target_wraps_inner_ssh(self):
        ssh = _FakeProc(stdout_data=b"hello")
        text, argv_lists, content = self._run_with(ssh)
        self.assertTrue(text.startswith("OK bytes=5"))
        self.assertEqual(content, b"hello")
        self.assertEqual(len(argv_lists), 1)
        self.assertEqual(argv_lists[0][0], "ssh")
        self.assertEqual(argv_lists[0][1], "dummy-bastion")
        self.assertEqual(argv_lists[0][2], "ssh")
        self.assertEqual(argv_lists[0][3], shlex.quote("app1"))
        self.assertEqual(argv_lists[0][4], "--")

    def test_parent_dir_missing_returns_error(self):
        ssh = _FakeProc(stdout_data=b"")
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return ssh

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            text = asyncio.run(
                exec_mod.get_file(
                    bastion="dummy-bastion",
                    target_host="app1",
                    remote_path="/etc/hostname",
                    local_path="/no/such/dir/out.bin",
                    session_id="get12345",
                    timeout_s=10,
                    cwd="/cwd",
                )
            )
        self.assertIn("ERROR: local parent directory does not exist", text)
        self.assertEqual(captured, [])

    def test_local_path_is_directory_returns_error(self):
        ssh = _FakeProc(stdout_data=b"")
        captured: list[tuple] = []

        async def _create(*args, **kw):
            captured.append(args)
            return ssh

        with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
            text = asyncio.run(
                exec_mod.get_file(
                    bastion="dummy-bastion",
                    target_host="app1",
                    remote_path="/etc/hostname",
                    local_path=self.tmp.name,
                    session_id="get12345",
                    timeout_s=10,
                    cwd="/cwd",
                )
            )
        self.assertIn("ERROR: local path exists but is not a regular file", text)
        self.assertEqual(captured, [])

    def test_happy_path_writes_local_file(self):
        payload = b"binary\x00data\xff" * 50
        ssh = _FakeProc(stdout_data=payload)
        text, _, content = self._run_with(ssh, remote_path="/etc/somefile")
        self.assertTrue(text.startswith(f"OK bytes={len(payload)}"))
        self.assertIn("app1:/etc/somefile", text)
        self.assertEqual(content, payload)

    def test_nonzero_exit_reports_failure(self):
        ssh = _FakeProc(stdout_data=b"", exit_code=1, stderr_lines=[b"cat: /nope: No such file\n"])
        text, _, _ = self._run_with(ssh, remote_path="/nope")
        self.assertTrue(text.startswith("FAILED"))
        self.assertIn("exit=1", text)
        self.assertIn("cat: /nope: No such file", text)

    def test_timeout_kills(self):
        ssh = _FakeProc(stdout_data=b"slow", wait_delay=10.0)
        text, _, _ = self._run_with(ssh, remote_path="/etc/slow", timeout_s=0.2)
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("timeout", text)
        self.assertTrue(ssh._terminated or ssh._killed)

    def test_external_cancel_terminates(self):
        ssh = _FakeProc(stdout_data=b"slow", wait_delay=10.0)
        cancel = asyncio.Event()

        async def runner():
            async def fire():
                await asyncio.sleep(0.2)
                cancel.set()

            fire_task = asyncio.create_task(fire())

            async def _create(*args, **kw):
                return ssh

            with tempfile.TemporaryDirectory() as outdir:
                local = str(Path(outdir) / "out.bin")
                with patch.object(exec_mod.asyncio, "create_subprocess_exec", side_effect=_create):
                    text = await exec_mod.get_file(
                        bastion="dummy-bastion",
                        target_host="app1",
                        remote_path="/etc/slow",
                        local_path=local,
                        session_id="get12345",
                        timeout_s=10,
                        cwd="/cwd",
                        cancel=cancel,
                    )
            fire_task.cancel()
            try:
                await fire_task
            except BaseException:
                pass
            return text

        text = asyncio.run(runner())
        self.assertTrue(text.startswith("CANCELLED"))
        self.assertIn("cancelled by client", text)
        self.assertTrue(ssh._terminated or ssh._killed)


if __name__ == "__main__":
    unittest.main()
