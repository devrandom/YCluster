import asyncio
import unittest
from unittest import mock

from starlette.requests import Request

from cluster_exec import exec as exec_mod, server


def _make_request(headers: dict[str, str]) -> Request:
    # Starlette Request accepts a raw ASGI scope; constructing it directly
    # avoids the overhead of spinning up a TestClient.
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/stop",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _run(coro):
    return asyncio.run(coro)


class StopEndpointAuthTests(unittest.TestCase):
    def tearDown(self):
        exec_mod._BACKGROUNDED.clear()

    def test_missing_authorization_returns_401(self):
        resp = _run(server._stop(_make_request({})))
        self.assertEqual(resp.status_code, 401)

    def test_wrong_scheme_returns_401(self):
        resp = _run(server._stop(_make_request({"authorization": "Basic abc"})))
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = _run(
            server._stop(_make_request({"authorization": "Bearer not-the-real-token"}))
        )
        self.assertEqual(resp.status_code, 401)

    def test_correct_token_kills_bg_and_signals(self):
        exec_mod._BACKGROUNDED.clear()
        # Register a fake backgrounded session so kill_all_backgrounded
        # has something to count.
        fake_proc = mock.MagicMock()
        fake_proc.returncode = None
        fake_proc.terminate.return_value = None

        async def register():
            watcher = asyncio.create_task(asyncio.sleep(0))
            exec_mod.register_backgrounded(
                session_id="stop00001",
                target_host="dummy",
                command="cmd",
                started=0.0,
                timeout_s=1800.0,
                proc=fake_proc,  # type: ignore[arg-type]
                readers=(asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))),
                timeout_watcher=watcher,
            )

        _run(register())

        # Verify SIGTERM scheduling rather than firing (call_later(0.5,...)
        # doesn't execute inside a short-lived test loop).
        with (
            mock.patch("cluster_exec.server.os.kill") as mock_kill,
            mock.patch.object(
                exec_mod, "close_ssh_master", new=mock.AsyncMock(return_value=True)
            ) as mock_close,
        ):
            resp = _run(
                server._stop(
                    _make_request({"authorization": f"Bearer {server._BEARER_TOKEN}"})
                )
            )
        self.assertEqual(resp.status_code, 200)
        mock_close.assert_awaited_once()
        # Backgrounded session was terminated before SIGTERM was scheduled,
        # but its terminal status is retained for the next daemon process.
        self.assertIn("stop00001", exec_mod.list_backgrounded())
        bg = exec_mod.get_backgrounded("stop00001")
        self.assertIsNotNone(bg)
        self.assertIsNotNone(bg.finished_at)
        self.assertEqual(bg.finished_reason, "stopped-with-daemon")
        self.assertTrue(fake_proc.terminate.called)
        # SIGTERM was scheduled (not yet fired) — call_later's callback
        # would call os.kill(pid, SIGTERM).
        mock_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
