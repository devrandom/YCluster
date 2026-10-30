import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from cluster_exec import exec as exec_mod
from cluster_exec import server


class _StubProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.pid = 4321
        self._terminated = False
        self._killed = False

    def terminate(self) -> None:
        self._terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self._killed = True
        if self.returncode is None:
            self.returncode = -9


def _stub_as_proc(s: _StubProcess):
    return cast(asyncio.subprocess.Process, s)


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        exec_mod._BACKGROUNDED.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "cluster-exec.state.json"
        exec_mod.init_state(self.state_path)

    def tearDown(self):
        exec_mod._BACKGROUNDED.clear()
        exec_mod._STATE_PATH = None
        exec_mod._STATE_LOADED = False
        self.tmp.cleanup()

    def _register(
        self,
        session_id: str,
        *,
        proc: _StubProcess | None = None,
        principal: str | None = "thread-1",
        timeout_s: float = 1800.0,
    ) -> None:
        async def setup():
            return exec_mod.register_backgrounded(
                session_id=session_id,
                target_host="dummy",
                command="cmd",
                started=0.0,
                timeout_s=timeout_s,
                proc=_stub_as_proc(proc or _StubProcess()),
                readers=(
                    asyncio.create_task(asyncio.sleep(0)),
                    asyncio.create_task(asyncio.sleep(0)),
                ),
                timeout_watcher=asyncio.create_task(asyncio.sleep(0)),
                principal=principal,
            )

        asyncio.run(setup())

    def _reload(self) -> None:
        # Simulate daemon exit: live handles vanish, then a new process reads
        # the same state file.
        exec_mod._BACKGROUNDED.clear()
        exec_mod.init_state(self.state_path, force=True)

    def test_finished_status_survives_restart(self):
        self._register("persist1")
        exec_mod._mark_backgrounded_finished("persist1", exit_code=7)

        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        snapshot = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["v"], 1)
        self.assertEqual(snapshot["sessions"][0]["exit_code"], 7)

        self._reload()
        bg = exec_mod.get_backgrounded("persist1")
        self.assertIsNotNone(bg)
        self.assertTrue(bg.recovered)
        self.assertIsNone(bg.proc)
        self.assertEqual(bg.exit_code, 7)
        text = asyncio.run(server.status("persist1"))[0].text
        self.assertNotIn("orphaned-by-restart", text)
        self.assertIn("exited exit=7", text)

    def test_running_status_survives_restart(self):
        self._register("persist2")
        self._reload()

        async def runner():
            with mock.patch.object(
                exec_mod,
                "_find_local_session_pid",
                new=mock.AsyncMock(return_value=9876),
            ):
                text = (await server.status("persist2"))[0].text
                await asyncio.sleep(0.05)
                return text

        text = asyncio.run(runner())
        self.assertIn("orphaned-by-restart", text)
        self.assertIn("running", text)
        bg = exec_mod.get_backgrounded("persist2")
        self.assertIsNotNone(bg)
        self.assertEqual(bg.ssh_pid, 9876)

    def test_recovered_session_kill_uses_local_termination(self):
        self._register("persist3")
        self._reload()
        with (
            mock.patch.object(exec_mod, "_local_session_pids", return_value=[1234]),
            mock.patch.object(
                exec_mod, "_terminate_local_session", return_value=True
            ) as terminate,
        ):
            self.assertTrue(exec_mod.kill_backgrounded("persist3"))
        terminate.assert_called_once_with("persist3", grace_s=2.0)
        self.assertIsNone(exec_mod.get_backgrounded("persist3"))

        snapshot = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["sessions"], [])

    def test_failed_recovered_kill_retains_status(self):
        self._register("persist6")
        self._reload()
        with (
            mock.patch.object(exec_mod, "_local_session_pids", return_value=[1234]),
            mock.patch.object(
                exec_mod, "_terminate_local_session", return_value=False
            ),
        ):
            self.assertFalse(exec_mod.kill_backgrounded("persist6"))
        bg = exec_mod.get_backgrounded("persist6")
        self.assertIsNotNone(bg)
        self.assertIsNone(bg.finished_at)

    def test_recovered_kill_with_no_process_retains_terminal_status(self):
        self._register("persist7")
        self._reload()
        with mock.patch.object(exec_mod, "_local_session_pids", return_value=[]):
            self.assertFalse(exec_mod.kill_backgrounded("persist7"))
        bg = exec_mod.get_backgrounded("persist7")
        self.assertIsNotNone(bg)
        self.assertIsNotNone(bg.finished_at)
        self.assertEqual(bg.finished_reason, "process-gone-after-daemon-restart")

    def test_daemon_stop_retains_terminal_status(self):
        self._register("persist4")
        self.assertEqual(exec_mod.kill_all_backgrounded(retain=True), 1)
        self.assertIn("persist4", exec_mod.list_backgrounded())

        self._reload()
        bg = exec_mod.get_backgrounded("persist4")
        self.assertIsNotNone(bg)
        self.assertIsNotNone(bg.finished_at)
        self.assertEqual(bg.finished_reason, "stopped-with-daemon")
        text = asyncio.run(server.status("persist4"))[0].text
        self.assertIn("reason=stopped-with-daemon", text)

    def test_malformed_state_is_ignored(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        exec_mod._BACKGROUNDED.clear()
        exec_mod._STATE_LOADED = False
        exec_mod.init_state(self.state_path)
        self.assertEqual(exec_mod.list_backgrounded(), [])

    def test_unsafe_session_id_is_rejected(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "v": 1,
                    "sessions": [
                        {
                            "session_id": ".*",
                            "target_host": "dummy",
                            "command": "cmd",
                            "started_wall": 0,
                            "timeout_s": 1800,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        exec_mod._BACKGROUNDED.clear()
        exec_mod._STATE_LOADED = False
        exec_mod.init_state(self.state_path)
        self.assertEqual(exec_mod.list_backgrounded(), [])

    def test_lifespan_loads_state_file(self):
        self._register("persist5")
        exec_mod._mark_backgrounded_finished("persist5", exit_code=0)
        exec_mod._BACKGROUNDED.clear()
        exec_mod._STATE_LOADED = False

        async def runner():
            with mock.patch.object(server, "_STATE_PATH", self.state_path):
                async with server._lifespan(None):
                    bp = exec_mod.get_backgrounded("persist5")
                    self.assertIsNotNone(bp)
                    self.assertTrue(bp.recovered)

        asyncio.run(runner())

    def test_recovered_watcher_marks_timeout_terminal(self):
        self._register("persist9", timeout_s=0.001)
        self._reload()

        async def runner():
            with (
                mock.patch.object(
                    exec_mod,
                    "_find_local_session_pid",
                    new=mock.AsyncMock(return_value=1234),
                ),
                mock.patch.object(
                    exec_mod, "_local_session_pids", return_value=[1234]
                ),
                mock.patch.object(
                    exec_mod, "_terminate_local_session", return_value=True
                ) as terminate,
            ):
                await exec_mod.start_recovered_watchers()
                await asyncio.sleep(0.1)
                terminate.assert_called_once_with("persist9")

        asyncio.run(runner())
        bg = exec_mod.get_backgrounded("persist9")
        self.assertIsNotNone(bg)
        self.assertIsNotNone(bg.finished_at)
        self.assertEqual(
            bg.finished_reason,
            "hard-timeout-after-daemon-restart",
        )

    def test_repeated_lifespan_keeps_live_handles(self):
        proc = _StubProcess()
        self._register("persist8", proc=proc)

        async def runner():
            with mock.patch.object(server, "_STATE_PATH", self.state_path):
                async with server._lifespan(None):
                    pass
                async with server._lifespan(None):
                    pass

        asyncio.run(runner())
        bg = exec_mod.get_backgrounded("persist8")
        self.assertIsNotNone(bg)
        self.assertIs(bg.proc, _stub_as_proc(proc))
        self.assertFalse(bg.recovered)


if __name__ == "__main__":
    unittest.main()
