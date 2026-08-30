#!/usr/bin/env python3
"""Unit tests for the ntfy-webhook dedup/throttle logic.

Run:  python3 -m unittest test_ntfy_webhook  (from this dir)
"""
import importlib.util
import os
import time as wh_time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ntfy_webhook", os.path.join(_HERE, "ntfy-webhook.py")
)
wh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wh)


def reset_state():
    wh._last_sent_at.clear()
    wh._send_history.clear()
    wh._shared_client = None
    os.environ["NTFY_SHARED_DEDUP"] = "0"


class AlertKey(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_uses_fingerprint(self):
        alert = {"status": "firing", "fingerprint": "abc123", "labels": {"alertname": "X"}}
        self.assertEqual(wh.alert_key(alert), "abc123")

    def test_marks_status_without_fingerprint(self):
        alert = {"status": "firing", "labels": {"alertname": "X", "node": "n1"}}
        firing = wh.alert_key(alert)
        resolved = wh.alert_key(dict(alert, status="resolved"))
        self.assertNotEqual(firing, resolved)


class Admit(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_first_send_admitted(self):
        self.assertEqual(wh._admit("k1", now=100.0), "send")

    def test_same_key_deduped_within_window(self):
        self.assertEqual(wh._admit("k1", now=100.0), "send")
        self.assertEqual(wh._admit("k1", now=100.0 + wh.DEDUP_WINDOW_SECONDS - 1), "dedup")

    def test_key_allowed_again_after_window(self):
        self.assertEqual(wh._admit("k1", now=100.0), "send")
        self.assertEqual(wh._admit("k1", now=100.0 + wh.DEDUP_WINDOW_SECONDS + 1), "send")

    def test_throttle_caps_distinct_sends(self):
        for i in range(wh.THROTTLE_MAX_SENDS):
            self.assertEqual(wh._admit(f"k{i}", now=200.0), "send")
        self.assertEqual(wh._admit("extra", now=200.0), "throttle")

    def test_throttle_releases_after_window(self):
        for i in range(wh.THROTTLE_MAX_SENDS):
            self.assertEqual(wh._admit(f"k{i}", now=200.0), "send")
        self.assertEqual(
            wh._admit("extra", now=200.0 + wh.THROTTLE_WINDOW_SECONDS + 1), "send"
        )


if __name__ == "__main__":
    unittest.main()


PAYLOAD = {
    "alerts": [
        {
            "status": "firing",
            "fingerprint": "fp-1",
            "labels": {"alertname": "TestAlert", "severity": "warning", "node": "n1"},
            "annotations": {"summary": "test", "description": "test desc"},
        }
    ]
}


def post(payload=PAYLOAD):
    client = wh.app.test_client()
    return client.post("/webhook", json=payload)


class SharedClaim(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_existing_cross_host_claim_is_rejected(self):
        class FakeEtcd:
            def lease(self, ttl):
                return object()

            def put_if_not_exists(self, key, value, lease=None):
                return False

        wh._shared_client = FakeEtcd()
        with unittest.mock.patch.dict(os.environ, {"NTFY_SHARED_DEDUP": "1"}):
            self.assertFalse(wh._shared_claim("fp-1"))

    def test_etcd_failure_fails_open(self):
        class FakeEtcd:
            def lease(self, ttl):
                raise RuntimeError("etcd unavailable")

        wh._shared_client = FakeEtcd()
        with unittest.mock.patch.dict(os.environ, {"NTFY_SHARED_DEDUP": "1"}):
            self.assertTrue(wh._shared_claim("fp-1"))

    def test_shared_client_fails_over_to_healthy_peer(self):
        probed = []

        def make_client(host, port, timeout, **kwargs):
            client = mock.MagicMock()
            if host == "down-host":
                client.status.side_effect = RuntimeError("unreachable")
            probed.append(host)
            return client

        fake_etcd3 = mock.MagicMock()
        fake_etcd3.client = make_client
        env = {
            "NTFY_SHARED_DEDUP": "1",
            "ETCD_HOSTS": "down-host:2381,healthy-host:2381",
        }
        with unittest.mock.patch.object(wh, "etcd3", fake_etcd3):
            with unittest.mock.patch.dict(os.environ, env):
                client = wh._new_shared_client()
        self.assertEqual(probed, ["down-host", "healthy-host"])
        self.assertIsNotNone(client)

    def test_shared_client_returns_none_when_all_peers_down(self):
        def make_client(host, port, timeout, **kwargs):
            client = mock.MagicMock()
            client.status.side_effect = RuntimeError("unreachable")
            return client

        fake_etcd3 = mock.MagicMock()
        fake_etcd3.client = make_client
        env = {
            "NTFY_SHARED_DEDUP": "1",
            "ETCD_HOSTS": "down-host:2381,other-down-host:2381",
        }
        with unittest.mock.patch.object(wh, "etcd3", fake_etcd3):
            with unittest.mock.patch.dict(os.environ, env):
                self.assertIsNone(wh._new_shared_client())


class WebhookHttp(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_success_returns_200(self):
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["sent"], 1)
        m.assert_called_once()

    def test_rate_limited_returns_200_not_503(self):
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value="rate_limited") as m:
            resp = post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["rate_limited"], 1)
        m.assert_called_once()

    def test_genuine_failure_returns_503_for_retry(self):
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=False):
            resp = post()
        self.assertEqual(resp.status_code, 503)

    def test_deduped_returns_200_without_calling_send(self):
        wh._admit("fp-1", now=wh_time.monotonic())
        with unittest.mock.patch.object(wh, "send_to_ntfy") as m:
            resp = post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["deduped"], 1)
        self.assertEqual(resp.get_json()["sent"], 0)
        m.assert_not_called()

    def test_repeated_webhook_from_ha_peer_is_deduped(self):
        with unittest.mock.patch.object(wh, "_shared_claim", side_effect=[True, False]):
            with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
                first = post()
                # Simulate the next Alertmanager HA peer's independent process.
                wh._last_sent_at.clear()
                wh._send_history.clear()
                second = post()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["sent"], 1)
        self.assertEqual(second.get_json()["deduped"], 1)
        m.assert_called_once()

    def test_throttled_returns_200_without_calling_send(self):
        for i in range(wh.THROTTLE_MAX_SENDS):
            wh._admit(f"other-{i}", now=wh_time.monotonic())
        with unittest.mock.patch.object(wh, "send_to_ntfy") as m:
            resp = post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["throttled"], 1)
        m.assert_not_called()
