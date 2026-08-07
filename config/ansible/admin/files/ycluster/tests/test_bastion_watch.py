"""Unit tests for the bastion-watch etcd watch resilience logic.

Pure helper tests only (no etcd / no incus): the helpers here are the code
that lets the long-running `ycluster vm bastion-watch` daemon recover when its
etcd3 watch stream dies instead of silently dropping future key changes.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest discover tests
or a single module:
    python3 -m unittest tests.test_bastion_watch
"""

import threading
import unittest

from ycluster.common import etcd_utils
from ycluster.utils import vm_manager as vm


class FakeEvent:
    def __init__(self, key):
        self.key = key if isinstance(key, bytes) else key.encode()


class WatchResponseKeysTests(unittest.TestCase):
    PREFIX = "/cluster/users/"

    def test_returned_keys_strip_prefix(self):
        resp = type("R", (), {"events": [
            FakeEvent(b"/cluster/users/alice@example.com"),
            FakeEvent(b"/cluster/users/bob@example.com"),
        ]})()
        self.assertEqual(
            vm._watch_response_keys(resp, self.PREFIX),
            ["alice@example.com", "bob@example.com"])

    def test_unrelated_events_skipped(self):
        resp = type("R", (), {"events": [
            FakeEvent(b"/cluster/users/alice@example.com"),
            FakeEvent(b"/cluster/vms/vm1"),
        ]})()
        self.assertEqual(vm._watch_response_keys(resp, self.PREFIX),
                         ["alice@example.com"])

    def test_broken_stream_response_is_none(self):
        # etcd3 0.12.x passes a grpc.RpcError (no `.events`) to the callback
        # when the watch stream breaks — that must read as "watch is dead",
        # not as an empty event batch.
        broken = type("RpcError", (), {})()
        self.assertIsNone(vm._watch_response_keys(broken, self.PREFIX))


def _alive_thread():
    t = threading.Thread(target=threading.Event().wait)
    t.daemon = True
    t.start()
    return t


class WatcherHealthyTests(unittest.TestCase):
    def test_all_watches_armed_is_healthy(self):
        watcher = type("W", (), {
            "_callback_thread": _alive_thread(),
            "_callbacks": {1: object(), 2: object()},
        })()
        self.assertTrue(vm._watcher_healthy([1, 2], watcher))

    def test_cleared_callbacks_is_unhealthy(self):
        # After a stream error etcd3 wipes _callbacks without re-arming.
        watcher = type("W", (), {
            "_callback_thread": _alive_thread(),
            "_callbacks": {},
        })()
        self.assertFalse(vm._watcher_healthy([1, 2], watcher))

    def test_missing_watch_is_unhealthy(self):
        watcher = type("W", (), {
            "_callback_thread": _alive_thread(),
            "_callbacks": {1: object()},
        })()
        self.assertFalse(vm._watcher_healthy([1, 2], watcher))

    def test_dead_watcher_thread_is_unhealthy(self):
        dead = threading.Thread(target=lambda: None)
        watcher = type("W", (), {
            "_callback_thread": dead,
            "_callbacks": {1: object(), 2: object()},
        })()
        self.assertFalse(vm._watcher_healthy([1, 2], watcher))

    def test_no_thread_is_unhealthy(self):
        watcher = type("W", (), {"_callbacks": {1: object(), 2: object()}})()
        self.assertFalse(vm._watcher_healthy([1, 2], watcher))

    def test_empty_watch_ids_is_unhealthy(self):
        watcher = type("W", (), {
            "_callback_thread": _alive_thread(),
            "_callbacks": {},
        })()
        self.assertFalse(vm._watcher_healthy([], watcher))


class ResetEtcdClientTests(unittest.TestCase):
    def test_reset_clears_cached_client(self):
        etcd_utils._CACHED_CLIENT = object()
        etcd_utils.reset_etcd_client()
        self.assertIsNone(etcd_utils._CACHED_CLIENT)
        etcd_utils._CACHED_CLIENT = None


if __name__ == "__main__":
    unittest.main()
