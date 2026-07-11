import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cluster_exec import cli, log


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        args = ["--log-path", str(self.path), *argv]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                rc = cli.main(args)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_default_uses_50_events(self):
        for i in range(60):
            log.line("s1", "stdout", f"line {i}")
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("tail=50", out)
        self.assertIn("line 59", out)
        self.assertNotIn("line 0\n", out)
        self.assertEqual(err, "")

    def test_custom_tail_lines(self):
        for i in range(20):
            log.line("s1", "stdout", f"line {i}")
        rc, out, _ = self._run("-n", "5")
        self.assertEqual(rc, 0)
        self.assertIn("tail=5", out)
        self.assertIn("line 19", out)
        self.assertNotIn("line 14", out)

    def test_session_filter(self):
        log.start("s1", "s4", "cmd-a", "/cwd")
        log.start("s2", "s5", "cmd-b", "/cwd")
        log.line("s1", "stdout", "from s1")
        log.line("s2", "stdout", "from s2")
        rc, out, _ = self._run("-s", "s1")
        self.assertEqual(rc, 0)
        self.assertIn("from s1", out)
        self.assertNotIn("from s2", out)

    def test_custom_log_path(self):
        alt = Path(self.tmp.name) / "alt.log"
        log.init(alt)
        log.line("alt-session", "stdout", "hello from alt")
        rc, out, _ = self._run("--log-path", str(alt))
        self.assertEqual(rc, 0)
        self.assertIn("hello from alt", out)
        self.assertIn(str(alt), out)

    def test_log_path_from_env(self):
        alt = Path(self.tmp.name) / "env.log"
        log.init(alt)
        log.line("env-session", "stdout", "hello from env")
        old = os.environ.get("CE_LOG_PATH")
        os.environ["CE_LOG_PATH"] = str(alt)
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main([])
            out = stdout.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("hello from env", out)
        finally:
            if old is None:
                os.environ.pop("CE_LOG_PATH", None)
            else:
                os.environ["CE_LOG_PATH"] = old

    def test_missing_log_file(self):
        rc, out, _ = self._run("--log-path", "/no/such/path-xyz.log")
        self.assertEqual(rc, 0)
        self.assertIn("(no events)", out)

    def test_negative_tail_lines_errors(self):
        rc, out, err = self._run("-n", "-1")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("ERROR: tail_lines must be non-negative", err)

    def test_help_flag(self):
        rc, out, _ = self._run("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Tail the cluster-exec JSONL log", out)
        self.assertIn("--tail-lines", out)
        self.assertIn("--session", out)
        self.assertIn("--follow", out)

    def test_negative_interval_errors(self):
        rc, out, err = self._run("-f", "--interval", "-1")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("ERROR: interval must be positive", err)


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_follow(self, **kwargs) -> tuple[io.StringIO, threading.Event, threading.Thread]:
        captured = io.StringIO()
        stop = threading.Event()
        kwargs.setdefault("path", self.path)
        kwargs.setdefault("n", 50)
        kwargs.setdefault("session", None)
        kwargs.setdefault("interval", 0.05)

        def runner() -> None:
            with contextlib.redirect_stdout(captured):
                cli.follow(stop_event=stop, **kwargs)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        return captured, stop, t

    def test_follow_prints_initial_tail(self):
        log.start("s1", "host", "cmd", "/cwd")
        log.line("s1", "stdout", "initial-line-1")
        log.line("s1", "stdout", "initial-line-2")
        captured, stop, t = self._run_follow()
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)
        self.assertIn("following", captured.getvalue())
        self.assertIn("initial-line-1", captured.getvalue())
        self.assertIn("initial-line-2", captured.getvalue())

    def test_follow_captures_appended_events(self):
        log.start("s1", "host", "cmd", "/cwd")
        log.line("s1", "stdout", "initial")
        captured, stop, t = self._run_follow()
        time.sleep(0.3)
        log.line("s1", "stdout", "appended-1")
        time.sleep(0.3)
        log.line("s1", "stdout", "appended-2")
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)
        out = captured.getvalue()
        self.assertIn("initial", out)
        self.assertIn("appended-1", out)
        self.assertIn("appended-2", out)

    def test_follow_session_filter_excludes_other_sessions(self):
        log.start("s1", "host", "cmd", "/cwd")
        log.line("s1", "stdout", "keep-this")
        captured, stop, t = self._run_follow(session="s1")
        time.sleep(0.3)
        log.line("s2", "stdout", "drop-this")
        log.line("s1", "stdout", "keep-also")
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)
        out = captured.getvalue()
        self.assertIn("keep-this", out)
        self.assertIn("keep-also", out)
        self.assertNotIn("drop-this", out)

    def test_follow_waits_for_missing_file(self):
        missing = Path(self.tmp.name) / "does-not-exist.log"
        captured, stop, t = self._run_follow(path=missing)
        time.sleep(0.2)
        log.init(missing)
        log.line("late", "stdout", "hello-late")
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)
        self.assertIn("hello-late", captured.getvalue())

    def test_follow_skips_malformed_lines(self):
        log.start("s1", "host", "cmd", "/cwd")
        log.line("s1", "stdout", "before")
        captured, stop, t = self._run_follow()
        time.sleep(0.3)
        with self.path.open("a", encoding="utf-8") as f:
            f.write("not-json-line\n")
        log.line("s1", "stdout", "after")
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)
        out = captured.getvalue()
        self.assertIn("before", out)
        self.assertIn("after", out)


if __name__ == "__main__":
    unittest.main()
