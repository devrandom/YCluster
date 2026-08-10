"""Tests for the lite health-api.

Exercises the helpers refactored out of admin-api: per-request cache
safety (flask.g vs a module global), is_core_node() replacing
get_core_nodes() (one etcd key, no cluster enumeration), and the
end-to-end /api/health route via Flask's test client.

No etcd/postgres server — etcd is faked via FakeEtcd.
"""

import importlib.util
import os
import socket
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fake_etcd import FakeEtcd                           # noqa: E402
from flask import g                                      # noqa: E402


def _request(ha):
    """Enter a request context with the per-request cache initialized.

    test_request_context() doesn't fire before_request hooks, so we seed
    g._cache manually — that's what the hook does in production.
    """
    ctx = ha.app.test_request_context()
    ctx.push()
    g._cache = {}
    return ctx

# Load health-api.py by file path. The module-level constants
# (_HOSTNAME, _IS_STORAGE_NODE) are derived from socket.gethostname(),
# so a non-storage hostname lets us exercise the non-storage short-
# circuit without monkeypatching.
_HEALTH_API = os.path.join(_FILES, "health-api.py")


def _load_as(hostname):
    """Reload health_api with socket.gethostname() returning `hostname`.

    Module-level constants are evaluated at import time, so the only way
    to flip them is to re-import under a mocked gethostname().
    """
    spec = importlib.util.spec_from_file_location("health_api", _HEALTH_API)
    assert spec is not None and spec.loader is not None
    ha = importlib.util.module_from_spec(spec)
    with mock.patch.object(socket, "gethostname", return_value=hostname):
        spec.loader.exec_module(ha)
    return ha


class IsCoreNodeTests(unittest.TestCase):
    """is_core_node() replaces get_core_nodes() with a single etcd key."""

    def _patch_etcd(self, etcd):
        """Patch the lazy import in health_api to return our fake."""
        fake_pkg = mock.MagicMock()
        fake_pkg.get_etcd_client = lambda: etcd
        return mock.patch.dict(sys.modules, {"ycluster.common.etcd_utils": fake_pkg})

    def test_non_storage_short_circuits_without_etcd(self):
        # c* nodes must NOT touch etcd (etcd is firewalled to s*).
        ha = _load_as("c1")
        self.assertFalse(ha._IS_STORAGE_NODE)
        self.assertFalse(ha._IS_ETCD_NODE)
        fake_etcd = mock.MagicMock()
        with self._patch_etcd(etcd=fake_etcd):
            with _request(ha):
                self.assertFalse(ha.is_core_node())
            # No etcd client construction when non-storage.
            self.assertFalse(fake_etcd.get_etcd_client.called)

    def test_storage_with_no_disabled_key_is_core(self):
        ha = _load_as("s1")
        self.assertTrue(ha._IS_STORAGE_NODE)
        self.assertTrue(ha._IS_ETCD_NODE)
        with self._patch_etcd(etcd=FakeEtcd()):
            with _request(ha):
                self.assertTrue(ha.is_core_node())

    def test_storage_with_disabled_false_is_core(self):
        etcd = FakeEtcd()
        etcd.store["/cluster/nodes/s1/disabled"] = "false"
        ha = _load_as("s1")
        with self._patch_etcd(etcd=etcd):
            with _request(ha):
                self.assertTrue(ha.is_core_node())

    def test_storage_with_disabled_true_is_not_core(self):
        etcd = FakeEtcd()
        etcd.store["/cluster/nodes/s1/disabled"] = "true"
        ha = _load_as("s1")
        with self._patch_etcd(etcd=etcd):
            with _request(ha):
                self.assertFalse(ha.is_core_node())


class PerRequestCacheTests(unittest.TestCase):
    """The cache must be request-scoped — leader changes between requests
    must be observed by the next request (would fail with a module-level
    dict that requires manual reset).
    """

    def _patch_etcd(self, etcd):
        fake_pkg = mock.MagicMock()
        fake_pkg.get_etcd_client = lambda: etcd
        return mock.patch.dict(sys.modules, {"ycluster.common.etcd_utils": fake_pkg})

    def test_leadership_change_observed_by_next_request(self):
        etcd = FakeEtcd()
        ha = _load_as("s1")

        # s1 is initially the leader.
        etcd.store["/cluster/leader/app"] = "s1"

        with self._patch_etcd(etcd=etcd):
            with _request(ha):
                self.assertTrue(ha.is_storage_leader())

            # Switch leadership mid-flight (between requests).
            etcd.store["/cluster/leader/app"] = "s2"

            with _request(ha):
                self.assertFalse(ha.is_storage_leader())

    def test_cache_does_not_leak_across_requests(self):
        # After the first request populates the cache, the second request
        # must start fresh — its leadership view must reflect current etcd.
        etcd = FakeEtcd()
        etcd.store["/cluster/leader/app"] = "s1"
        ha = _load_as("s1")

        with self._patch_etcd(etcd=etcd):
            with _request(ha):
                self.assertTrue(ha.is_storage_leader())
                # Cache populated for THIS request.

            # Mutate outside of any request context.
            etcd.store["/cluster/leader/app"] = "s2"

            with _request(ha):
                # New request sees new leader — cache didn't leak.
                self.assertFalse(ha.is_storage_leader())


class HealthRouteTests(unittest.TestCase):
    """End-to-end via Flask's test client. Exercises the public surface
    that nginx fronts; if the cache hygiene or is_core_node refactor
    broke anything observable, this catches it.
    """

    def _patch_etcd(self, etcd):
        fake_pkg = mock.MagicMock()
        fake_pkg.get_etcd_client = lambda: etcd
        return mock.patch.dict(sys.modules, {"ycluster.common.etcd_utils": fake_pkg})

    def test_non_storage_node_health_route(self):
        ha = _load_as("c1")
        client = ha.app.test_client()
        # Speed the route up by stubbing the slow/IO-heavy checks. The
        # test isn't validating those checks (they're exercised on real
        # nodes); it's validating that the route is wired, returns JSON
        # with the expected service count, and keeps the is_core_node
        # logic consistent for keepalived on non-storage nodes.
        #   - check_service_status: subprocess.run on systemctl (fails
        #     fast but slow when no dbus in the test env). Default False
        #     so non-storage keepalived reads as 'standby', not split-
        #     brain.
        #   - check_port_open / check_dns_status / check_clock_skew: each
        #     has multi-second timeouts (DNS=5s, NTP=2s) that blow up the
        #     suite without these stubs.
        with mock.patch.object(ha, "check_service_status", return_value=False), \
             mock.patch.object(ha, "check_port_open", return_value=False), \
             mock.patch.object(ha, "check_dns_status",
                              return_value={"status": "healthy", "details": {}}), \
             mock.patch.object(ha, "check_clock_skew",
                              return_value={"status": "healthy", "details": {}}), \
             self._patch_etcd(etcd=FakeEtcd()):
            r1 = client.get("/api/health")
            r2 = client.get("/api/health")
        # Two requests, each well-formed (200 or 503). Test env lacks the
        # real services so overall can be unhealthy; the route must
        # always respond with JSON.
        for r in (r1, r2):
            self.assertIn(r.status_code, (200, 503))
            data = r.get_json()
            self.assertEqual(len(data["services"]), 20)
            self.assertIn("keepalived", data["services"])
            # c1 is non-storage → keepalived must be standby (not healthy,
            # not split-brain). This is the is_core_node() check end-to-end.
            self.assertEqual(data["services"]["keepalived"]["status"], "standby")


if __name__ == "__main__":
    unittest.main()
