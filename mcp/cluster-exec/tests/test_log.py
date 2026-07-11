import json
import tempfile
import threading
import unittest
from pathlib import Path

from cluster_exec import log


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def test_basic_events(self):
        log.start("s1", "s4", "uptime", "/cwd")
        log.line("s1", "stdout", "line one")
        log.line("s1", "stderr", "warning")
        log.end("s1", 0, 1.234, "ok", lines=2)

        events = self._read()
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[0]["session"], "s1")
        self.assertEqual(events[0]["host"], "s4")
        self.assertEqual(events[1]["event"], "line")
        self.assertEqual(events[1]["stream"], "stdout")
        self.assertEqual(events[2]["stream"], "stderr")
        self.assertEqual(events[3]["event"], "end")
        self.assertEqual(events[3]["status"], "ok")
        self.assertEqual(events[3]["lines"], 2)
        self.assertEqual(events[3]["duration_s"], 1.234)
        for e in events:
            self.assertIn("ts", e)
            self.assertTrue(e["ts"].endswith("Z"))

    def test_concurrent_writes_no_torn_lines(self):
        n_threads = 8
        per_thread = 200
        big = "x" * 5000  # > PIPE_BUF

        def worker(idx: int) -> None:
            for i in range(per_thread):
                log.line(f"s{idx}", "stdout", big)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = self._read()
        self.assertEqual(len(events), n_threads * per_thread)
        for e in events:
            self.assertEqual(len(e["line"]), 5000)
            self.assertTrue(all(c == "x" for c in e["line"]))

    def test_end_with_error(self):
        log.end("s1", None, 0.5, "error", lines=0, error="ssh: refused")
        events = self._read()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error"], "ssh: refused")


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cluster.log"
        log.init(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_file(self):
        self.assertEqual(log.tail(self.path, n=10), [])

    def test_missing_file(self):
        self.assertEqual(log.tail(Path("/no/such/path-xyz.log"), n=10), [])

    def test_returns_last_n(self):
        for i in range(20):
            log.line("s1", "stdout", f"line {i}")
        events = log.tail(self.path, n=5)
        self.assertEqual(len(events), 5)
        self.assertEqual([e["line"] for e in events],
                         ["line 15", "line 16", "line 17", "line 18", "line 19"])

    def test_n_larger_than_file(self):
        for i in range(3):
            log.line("s1", "stdout", f"line {i}")
        events = log.tail(self.path, n=100)
        self.assertEqual(len(events), 3)

    def test_n_zero(self):
        for i in range(3):
            log.line("s1", "stdout", f"line {i}")
        self.assertEqual(log.tail(self.path, n=0), [])

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            log.tail(self.path, n=-1)

    def test_session_filter(self):
        log.start("s1", "s4", "cmd-a", "/cwd")
        log.start("s2", "s5", "cmd-b", "/cwd")
        log.line("s1", "stdout", "from s1 a")
        log.line("s2", "stdout", "from s2 a")
        log.line("s1", "stdout", "from s1 b")
        events = log.tail(self.path, n=100, session="s1")
        self.assertTrue(all(e["session"] == "s1" for e in events))
        self.assertEqual(len(events), 3)

    def test_session_filter_with_tail(self):
        for i in range(10):
            log.line("s1" if i % 2 else "s2", "stdout", f"line {i}")
        events = log.tail(self.path, n=3, session="s1")
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["session"] == "s1" for e in events))

    def test_skips_malformed_lines(self):
        self.path.write_text(
            '{"ts":"2026-01-01T00:00:00.000Z","event":"start","session":"s1","host":"s4","cmd":"uptime","cwd":"/cwd"}\n'
            "this is not json\n"
            '{"ts":"2026-01-01T00:00:00.001Z","event":"line","session":"s1","stream":"stdout","line":"hi"}\n'
        )
        events = log.tail(self.path, n=10)
        self.assertEqual(len(events), 2)


class RenderTests(unittest.TestCase):
    def test_no_events(self):
        text = log.render([], path=Path("x"), requested=10)
        self.assertIn("[tail] 0 events", text)
        self.assertIn("(no events)", text)

    def test_singular_label(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "start",
                   "session": "abc", "host": "s4", "cmd": "uptime", "cwd": "/cwd"}]
        text = log.render(events, path=Path("x"), requested=10)
        self.assertIn("[tail] 1 event ", text)

    def test_header_includes_path_and_tail(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "start",
                   "session": "abc", "host": "s4", "cmd": "uptime", "cwd": "/cwd"}]
        text = log.render(events, path=Path("tmp/x.log"), requested=25)
        self.assertIn("tmp/x.log", text)
        self.assertIn("tail=25", text)

    def test_start_event(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "start",
                   "session": "abc", "host": "s4", "cmd": "uptime", "cwd": "/cwd"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("[abc]", text)
        self.assertIn("start", text)
        self.assertIn("host=s4", text)
        self.assertIn("cmd='uptime'", text)
        self.assertIn("cwd=/cwd", text)

    def test_start_event_no_cwd(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "start",
                   "session": "abc", "host": "s4", "cmd": "uptime"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertNotIn("cwd=", text)

    def test_line_stdout(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "line",
                   "session": "abc", "stream": "stdout", "line": "hello"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("hello", text)
        self.assertNotIn("[stderr]", text)

    def test_line_stderr(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "line",
                   "session": "abc", "stream": "stderr", "line": "warn"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("[stderr] warn", text)

    def test_end_event(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "end",
                   "session": "abc", "exit": 0, "duration_s": 1.5,
                   "status": "ok", "lines": 3}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("end", text)
        self.assertIn("exit=0", text)
        self.assertIn("duration=1.5s", text)
        self.assertIn("status=ok", text)
        self.assertIn("lines=3", text)

    def test_end_event_with_error(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "end",
                   "session": "abc", "exit": None, "duration_s": 0.0,
                   "status": "error", "lines": 0, "error": "boom"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("error='boom'", text)

    def test_unknown_event(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "foo",
                   "session": "abc"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("???", text)
        self.assertIn("event=foo", text)

    def test_missing_fields_render_question_marks(self):
        events = [{"ts": "2026-01-01T00:00:00.000Z", "event": "end"}]
        text = log.render(events, path=Path("x"), requested=1)
        self.assertIn("[?]", text)
        self.assertIn("exit=?", text)
        self.assertIn("status=?", text)


if __name__ == "__main__":
    unittest.main()
