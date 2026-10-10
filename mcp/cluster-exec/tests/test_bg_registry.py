import asyncio
import unittest
from typing import cast

from mcp.server.mcpserver.context import Context
from starlette.requests import Request

from cluster_exec import exec as exec_mod
from cluster_exec import server


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

    def _register(self, session_id: str, proc: _StubProcess, principal: str | None = None) -> asyncio.Task:
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
                principal=principal,
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

    def test_kill_all_terminates_every_session(self):
        procs = [_StubProcess(), _StubProcess(), _StubProcess()]
        for i, p in enumerate(procs):
            self._register(f"reg{i:05d}", p)
        count = exec_mod.kill_all_backgrounded()
        self.assertEqual(count, 3)
        for p in procs:
            self.assertTrue(p._terminated)
        self.assertEqual(exec_mod.list_backgrounded(), [])

    def test_kill_all_on_empty_registry_returns_zero(self):
        self.assertEqual(exec_mod.kill_all_backgrounded(), 0)

    def test_list_filtered_by_principal(self):
        # Two clients, each with their own session. list_backgrounded
        # scoped to a principal only returns that client's sessions.
        self._register("cl1s00001", _StubProcess(), principal="client-1")
        self._register("cl1s00002", _StubProcess(), principal="client-1")
        self._register("cl2s00001", _StubProcess(), principal="client-2")
        self._register("anons0001", _StubProcess(), principal=None)

        self.assertEqual(
            sorted(exec_mod.list_backgrounded(principal="client-1")),
            ["cl1s00001", "cl1s00002"],
        )
        self.assertEqual(
            exec_mod.list_backgrounded(principal="client-2"),
            ["cl2s00001"],
        )
        # Unfiltered (admin / legacy) sees everything.
        self.assertEqual(
            sorted(exec_mod.list_backgrounded()),
            ["anons0001", "cl1s00001", "cl1s00002", "cl2s00001"],
        )
        # Filtering for an unknown principal returns empty.
        self.assertEqual(exec_mod.list_backgrounded(principal="client-9"), [])

    def test_get_filtered_by_principal(self):
        proc = _StubProcess()
        self._register("ownd00001", proc, principal="client-1")
        self._register("othrd0001", _StubProcess(), principal="client-2")

        # Owner sees their own session.
        self.assertIsNotNone(exec_mod.get_backgrounded("ownd00001", principal="client-1"))
        # Other client gets None (no enumeration via session_id).
        self.assertIsNone(exec_mod.get_backgrounded("ownd00001", principal="client-2"))
        # Unfiltered (admin) sees all.
        self.assertIsNotNone(exec_mod.get_backgrounded("ownd00001"))
        self.assertIsNotNone(exec_mod.get_backgrounded("othrd0001"))

    def test_kill_filtered_by_principal_does_not_cross_clients(self):
        # A client trying to kill another client's session gets False
        # (session is still alive and in the registry).
        own_proc = _StubProcess()
        other_proc = _StubProcess()
        self._register("ownd00002", own_proc, principal="client-1")
        self._register("othrd0002", other_proc, principal="client-2")

        # Wrong principal: kill_backgrounded returns False, proc untouched.
        self.assertFalse(exec_mod.kill_backgrounded("othrd0002", principal="client-1"))
        self.assertFalse(other_proc._terminated)
        self.assertIn("othrd0002", exec_mod.list_backgrounded())

        # Right principal: kill_backgrounded returns True, proc terminated.
        self.assertTrue(exec_mod.kill_backgrounded("ownd00002", principal="client-1"))
        self.assertTrue(own_proc._terminated)
        self.assertNotIn("ownd00002", exec_mod.list_backgrounded())

    def test_kill_all_backgrounded_bypasses_principal_filter(self):
        # /stop endpoint is an admin op — it must clean up sessions owned
        # by any principal (including disconnected clients' orphans).
        procs = [_StubProcess(), _StubProcess(), _StubProcess()]
        self._register("admns0001", procs[0], principal="client-1")
        self._register("admns0002", procs[1], principal="client-2")
        self._register("admns0003", procs[2], principal=None)
        count = exec_mod.kill_all_backgrounded()
        self.assertEqual(count, 3)
        for p in procs:
            self.assertTrue(p._terminated)
        self.assertEqual(exec_mod.list_backgrounded(), [])


class SessionByIDCrossPrincipalTests(unittest.TestCase):
    """By-ID `status` and `kill` are not scoped to the caller's thread: an
    agent handed a `session_id` can inspect and terminate another thread's
    run. Only the no-arg `status` listing is scoped to the caller's thread."""

    def setUp(self):
        exec_mod._BACKGROUNDED.clear()

    def tearDown(self):
        exec_mod._BACKGROUNDED.clear()

    def _register(self, session_id: str, principal: str | None, proc: _StubProcess | None = None) -> None:
        async def setup():
            watcher = asyncio.create_task(asyncio.sleep(0))
            exec_mod.register_backgrounded(
                session_id=session_id,
                target_host="dummy",
                command="cmd",
                started=0.0,
                timeout_s=1800.0,
                proc=_stub_as_proc(proc or _StubProcess()),
                readers=(asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))),
                timeout_watcher=watcher,
                principal=principal,
            )
            return watcher
        _run(setup())

    @staticmethod
    def _ctx(thread_id: str):
        # Minimal Context that plumbs `_meta.threadId` through, so
        # `_principal_from_ctx` resolves the caller's principal.
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
        request = Request(scope)

        class _RC:
            request: Request | None = None
            meta: dict | None = None

        rc = _RC()
        rc.request = request
        rc.meta = {"threadId": thread_id}
        return Context(request_context=rc)

    def test_status_by_id_sees_another_threads_session(self):
        self._register("statusA0001", "thread-1")
        self._register("statusB0001", "thread-2")
        text = _run(server.status("statusB0001"))[0].text
        self.assertIn("statusB0001", text)
        self.assertIn("host=dummy", text)

    def test_status_by_id_shows_finished_session(self):
        proc = _StubProcess()
        proc.returncode = 0
        self._register("statusF0001", "thread-1", proc)
        text = _run(server.status("statusF0001"))[0].text
        self.assertIn("statusF0001", text)
        self.assertIn("exited exit=0", text)

    def test_status_listing_is_scoped_to_own_thread(self):
        self._register("statusA0002", "thread-1")
        self._register("statusB0002", "thread-2")
        text = _run(server.status(ctx=self._ctx("thread-1")))[0].text
        self.assertIn("1 backgrounded session(s)", text)
        self.assertIn("statusA0002", text)
        self.assertNotIn("statusB0002", text)

    def test_kill_cross_thread(self):
        proc = _StubProcess()
        async def setup():
            watcher = asyncio.create_task(asyncio.sleep(0))
            exec_mod.register_backgrounded(
                session_id="killerB001",
                target_host="dummy",
                command="cmd",
                started=0.0,
                timeout_s=1800.0,
                proc=_stub_as_proc(proc),
                readers=(asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))),
                timeout_watcher=watcher,
                principal="thread-2",
            )
        _run(setup())
        text = _run(server.kill("killerB001"))[0].text
        self.assertTrue(text.startswith("KILLED session=killerB001"), text)
        self.assertTrue(proc._terminated)
        self.assertNotIn("killerB001", exec_mod.list_backgrounded())


if __name__ == "__main__":
    unittest.main()
