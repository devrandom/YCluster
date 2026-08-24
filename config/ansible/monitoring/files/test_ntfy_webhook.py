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

    def test_throttled_returns_200_without_calling_send(self):
        for i in range(wh.THROTTLE_MAX_SENDS):
            wh._admit(f"other-{i}", now=wh_time.monotonic())
        with unittest.mock.patch.object(wh, "send_to_ntfy") as m:
            resp = post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["throttled"], 1)
        m.assert_not_called()
