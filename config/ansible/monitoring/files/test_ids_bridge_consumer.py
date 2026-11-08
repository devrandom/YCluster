#!/usr/bin/env python3
"""Unit tests for the VM bridge guard log consumer."""

import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ids_bridge_consumer", _HERE / "ids-bridge-consumer.py"
)
con = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(con)

MAPPING = {"10.100.0.11": "loud", "10.100.0.12": "quiet"}


def guard_line(cls, src="10.100.0.11", dst="10.100.0.53"):
    return (
        "node kernel: vm-bridge-guard {} IN=tap0 OUT=veth1 "
        "MAC=00:16:3e:00:00:01 SRC={} DST={} LEN=60 PROTO=TCP SYN\n"
    ).format(cls, src, dst)


def state(quiet="", **kwargs):
    quiet_path = ""
    if quiet:
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write(quiet)
        handle.close()
        quiet_path = handle.name
    index = con.InstanceIndex(lister=lambda: MAPPING)
    return con.State(quiet_path, index=index, **kwargs)


class IdsBridgeConsumerTests(unittest.TestCase):
    def test_parse_line_extracts_class_and_source(self):
        self.assertEqual(con.parse_line(guard_line("neighbour")),
                         ("neighbour", "10.100.0.11"))

    def test_parse_line_ignores_unrelated_lines(self):
        self.assertEqual(con.parse_line("node kernel: eth0: link up"),
                         (None, None))

    def test_payload_carries_no_addresses(self):
        body = json.dumps(con.payload("neighbour", "node1", "guest1"))
        for leak in ("10.100.0.11", "10.100.0.53", "veth", "tap"):
            self.assertNotIn(leak, body)
        self.assertIn("guest1", body)

    def test_payload_shape_matches_receiver(self):
        item = con.payload("cluster", "node1")[0]
        self.assertEqual(item["scenario"], "vm-bridge-cluster-egress")
        self.assertEqual(item["machine"], {"hostname": "node1"})
        self.assertEqual(item["events"], [{
            "message": "guest tried to reach a cluster network through the VM bridge",
        }])

    def test_limiter_allows_one_per_window(self):
        now = [0.0]
        limiter = con.RateLimiter(burst=1, window=3600, clock=lambda: now[0])
        self.assertTrue(limiter.allow())
        now[0] = 60.0
        self.assertFalse(limiter.allow())
        now[0] = 3601.0
        self.assertTrue(limiter.allow())

    def test_budget_is_per_instance(self):
        # A noisy guest must not consume another guest's first alert.
        now = [0.0]
        st = state(clock=lambda: now[0])
        sent = []
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            self.assertTrue(con.handle_line(guard_line("cluster", "10.100.0.11"),
                                            "http://x", "node", st))
            self.assertFalse(con.handle_line(guard_line("cluster", "10.100.0.11"),
                                             "http://x", "node", st))
            self.assertTrue(con.handle_line(guard_line("cluster", "10.100.0.12"),
                                            "http://x", "node", st))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0]["events"][0]["message"].split(":")[0], "loud")
        self.assertEqual(sent[1][0]["events"][0]["message"].split(":")[0], "quiet")

    def test_quiet_instance_is_not_alerted(self):
        now = [0.0]
        st = state(quiet="quiet\n", clock=lambda: now[0])
        sent = []
        out = io.StringIO()
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            with redirect_stdout(out):
                self.assertFalse(con.handle_line(guard_line("neighbour", "10.100.0.12"),
                                                 "http://x", "node", st))
                self.assertFalse(con.handle_line(guard_line("neighbour", "10.100.0.12"),
                                                 "http://x", "node", st))
        self.assertEqual(sent, [])
        self.assertIn("quieted: neighbour from quiet", out.getvalue())
        # One audit line per guest per class per window, not one per packet.
        self.assertEqual(out.getvalue().count("quieted:"), 1)

    def test_unknown_source_still_alerts(self):
        st = state()
        sent = []
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            self.assertTrue(con.handle_line(guard_line("cluster", "10.100.0.99"),
                                            "http://x", "node", st))
        self.assertEqual(len(sent), 1)
        self.assertNotIn(":", sent[0][0]["events"][0]["message"])

    def test_handle_line_ignores_unrelated_lines(self):
        sent = []
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            self.assertFalse(con.handle_line("node kernel: something else",
                                             "http://x", "node", state()))
        self.assertEqual(sent, [])

    def test_handle_line_survives_receiver_down(self):
        with mock.patch.object(con, "post", side_effect=urllib.error.URLError("down")):
            self.assertFalse(con.handle_line(guard_line("cluster"), "http://x",
                                             "node", state()))


if __name__ == "__main__":
    unittest.main()
