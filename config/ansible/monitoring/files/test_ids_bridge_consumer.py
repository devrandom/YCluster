#!/usr/bin/env python3
"""Unit tests for the VM bridge guard log consumer."""

import importlib.util
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ids_bridge_consumer", _HERE / "ids-bridge-consumer.py"
)
con = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(con)


def guard_line(cls, src="10.100.0.10", dst="10.100.0.53"):
    return (
        "node kernel: vm-bridge-guard {} IN=tap0 OUT=veth1 "
        "MAC=00:16:3e:00:00:01 SRC={} DST={} LEN=60 PROTO=TCP SYN\n"
    ).format(cls, src, dst)


class IdsBridgeConsumerTests(unittest.TestCase):
    def _limiters(self):
        return {cls: con.RateLimiter() for cls in con.CLASSES}

    def test_classify_neighbour(self):
        self.assertEqual(con.classify(guard_line("neighbour")), "neighbour")

    def test_classify_cluster(self):
        self.assertEqual(con.classify(guard_line("cluster")), "cluster")

    def test_classify_ignores_unrelated_lines(self):
        self.assertIsNone(con.classify("node kernel: eth0: link up"))

    def test_payload_carries_no_addresses(self):
        body = json.dumps(con.payload("neighbour", "node1"))
        for leak in ("10.100.0.10", "10.100.0.53", "veth", "tap"):
            self.assertNotIn(leak, body)

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

    def test_handle_line_posts_once_per_window(self):
        now = [0.0]
        limiters = {
            cls: con.RateLimiter(burst=1, window=3600, clock=lambda: now[0])
            for cls in con.CLASSES
        }
        sent = []
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            self.assertTrue(con.handle_line(guard_line("neighbour"),
                                            "http://127.0.0.1:1", "node1", limiters))
            now[0] = 5.0
            self.assertFalse(con.handle_line(guard_line("neighbour"),
                                             "http://127.0.0.1:1", "node1", limiters))
            self.assertTrue(con.handle_line(guard_line("cluster"),
                                            "http://127.0.0.1:1", "node1", limiters))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1][0]["scenario"], "vm-bridge-cluster-egress")
        self.assertEqual(sent[0][0]["scenario"], "vm-bridge-neighbour-egress")

    def test_handle_line_ignores_unrelated_lines(self):
        sent = []
        with mock.patch.object(con, "post", side_effect=lambda url, body: sent.append(body)):
            self.assertFalse(con.handle_line("node kernel: something else",
                                             "http://127.0.0.1:1", "node",
                                             self._limiters()))
        self.assertEqual(sent, [])

    def test_handle_line_survives_receiver_down(self):
        with mock.patch.object(con, "post", side_effect=urllib.error.URLError("down")):
            self.assertFalse(con.handle_line(guard_line("cluster"),
                                             "http://127.0.0.1:1", "node1",
                                             self._limiters()))


if __name__ == "__main__":
    unittest.main()
