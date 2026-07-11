import asyncio
import unittest
from typing import cast

from cluster_exec import exec as exec_mod


class _StubProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
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


def _run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        exec_mod._BACKGROUNDED.clear()

    def tearDown(self):
        exec_mod._BACKGROUNDED.clear()

    def _register(self, session_id: str, proc: _StubProcess) -> asyncio.Task:
        async def setup():
            watcher = asyncio.create_task(asyncio.sleep(0))
            exec_mod.register_backgrounded(
                session_id=session_id,
                target_host="dummy",
                command="cmd",
                started=0.0,
                timeout_s=1800.0,
                proc=_stub_as_proc(proc),
                readers=(asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))),
                timeout_watcher=watcher,
            )
            return watcher
        return _run(setup())

    def test_register_and_list(self):
        proc = _StubProcess()
        self._register("reg00001", proc)
        self.assertEqual(exec_mod.list_backgrounded(), ["reg00001"])

    def test_kill_terminates_and_removes(self):
        proc = _StubProcess()
        watcher = self._register("reg00002", proc)
        ok = exec_mod.kill_backgrounded("reg00002")
        self.assertTrue(ok)
        self.assertTrue(proc._terminated)
        self.assertEqual(exec_mod.list_backgrounded(), [])
        self.assertTrue(watcher.cancelled() or watcher.done())

    def test_kill_unknown_returns_false(self):
        self.assertFalse(exec_mod.kill_backgrounded("nope"))

    def test_kill_already_exited_is_noop(self):
        proc = _StubProcess()
        proc.returncode = 0
        self._register("reg00003", proc)
        ok = exec_mod.kill_backgrounded("reg00003")
        self.assertTrue(ok)
        self.assertFalse(proc._terminated)
        self.assertEqual(exec_mod.list_backgrounded(), [])


if __name__ == "__main__":
    unittest.main()
