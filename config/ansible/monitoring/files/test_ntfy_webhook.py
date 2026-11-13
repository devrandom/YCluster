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
        self.assertEqual(wh.alert_key(alert), "firing:abc123")

    def test_marks_status_without_fingerprint(self):
        alert = {"status": "firing", "labels": {"alertname": "X", "node": "n1"}}
        firing = wh.alert_key(alert)
        resolved = wh.alert_key(dict(alert, status="resolved"))
        self.assertNotEqual(firing, resolved)

    def test_fingerprint_status_allows_resolution_notification(self):
        alert = {"status": "firing", "fingerprint": "abc123", "labels": {"alertname": "X"}}
        self.assertNotEqual(wh.alert_key(alert), wh.alert_key(dict(alert, status="resolved")))


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


class FakeLease:
    def revoke(self):
        pass


class FakeEtcd:
    def __init__(self):
        self.keys = set()

    def lease(self, ttl):
        return FakeLease()

    def get(self, key):
        return (b"1", None) if key in self.keys else (None, None)

    def put_if_not_exists(self, key, value, lease=None):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def delete(self, key):
        self.keys.discard(key)


class IdsBudget(unittest.TestCase):
    def setUp(self):
        reset_state()
        wh.IDS_RATE_LIMIT = 2
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"

    def test_budget_is_per_severity(self):
        self.assertTrue(wh._ids_budget_claim("warning", now=100.0))
        self.assertTrue(wh._ids_budget_claim("warning", now=100.0))
        self.assertFalse(wh._ids_budget_claim("warning", now=100.0))
        self.assertTrue(wh._ids_budget_claim("critical", now=100.0))

    def test_budget_expires_in_next_window(self):
        self.assertTrue(wh._ids_budget_claim("warning", now=100.0))
        self.assertTrue(wh._ids_budget_claim("warning", now=100.0))
        self.assertFalse(wh._ids_budget_claim("warning", now=100.0))
        self.assertTrue(wh._ids_budget_claim("warning", now=100.0 + 3600))

    def test_cap_notice_is_once_per_severity_and_window(self):
        self.assertIsNotNone(wh._ids_cap_alert_claim("warning", now=100.0))
        self.assertIsNone(wh._ids_cap_alert_claim("warning", now=100.0))
        self.assertIsNotNone(wh._ids_cap_alert_claim("critical", now=100.0))
        self.assertIsNotNone(wh._ids_cap_alert_claim("warning", now=100.0 + 3600))

    def test_budget_fails_open_without_etcd(self):
        wh._shared_client = None
        with mock.patch.object(wh, "_new_shared_client", return_value=None):
            self.assertTrue(wh._ids_budget_claim("warning", now=100.0))


class Routing(unittest.TestCase):
    """Which topic an alert goes to, and how send_to_ntfy resolves it."""

    def setUp(self):
        reset_state()
        self.env = mock.patch.dict(
            os.environ,
            {
                "NTFY_TOPIC_MAIN": "main-topic",
                "NTFY_TOPIC_IDS": "ids-topic",
                "NTFY_LOCATIONS": '{"lab": {"topic": "lab-topic", "hosts": ["lab1", "lab2"]}}',
                "NTFY_HOST_TOPICS": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_ids_alert_uses_the_ids_topic(self):
        alert = {"labels": {"alertname": "IDSAlert", "severity": "critical"}}
        self.assertEqual(wh.alert_topic(alert), "ids-topic")

    def test_ids_wins_over_a_host_route(self):
        alert = {"labels": {"alertname": "IDSAlert", "node": "lab1"}}
        self.assertEqual(wh.alert_topic(alert), "ids-topic")

    def test_ids_coverage_alert_uses_the_ids_topic(self):
        alert = {"labels": {"alertname": "TetragonDown", "node": "lab1", "ids_coverage": "true"}}
        self.assertEqual(wh.alert_topic(alert), "ids-topic")

    def test_coverage_alert_keeps_the_host_topic_without_the_label(self):
        alert = {"labels": {"alertname": "TetragonDown", "node": "lab1"}}
        self.assertEqual(wh.alert_topic(alert), "lab-topic")

    def test_host_routes_through_its_location(self):
        alert = {"labels": {"alertname": "NodeDown", "node": "lab2"}}
        self.assertEqual(wh.alert_topic(alert), "lab-topic")

    def test_host_route_matches_every_label_form(self):
        for value in (
            "lab1:9100",
            "lab1.xc:9218",
            "https://lab1.xc:9218",
            "lab1.example.net",
        ):
            alert = {"labels": {"alertname": "X", "instance": value}}
            self.assertEqual(wh.alert_topic(alert), "lab-topic", value)

    def test_direct_host_topic_wins_over_the_location(self):
        os.environ["NTFY_HOST_TOPICS"] = '{"lab1": "solo-topic"}'
        alert = {"labels": {"alertname": "NodeDown", "node": "lab1"}}
        self.assertEqual(wh.alert_topic(alert), "solo-topic")
        other = {"labels": {"alertname": "NodeDown", "node": "lab2"}}
        self.assertEqual(wh.alert_topic(other), "lab-topic")

    def test_routed_host_needs_no_location(self):
        os.environ["NTFY_LOCATIONS"] = ""
        os.environ["NTFY_HOST_TOPICS"] = '{"lab1": "solo-topic"}'
        alert = {"labels": {"alertname": "NodeDown", "node": "lab1"}}
        self.assertEqual(wh.alert_topic(alert), "solo-topic")

    def test_similar_hostname_is_not_a_match(self):
        alert = {"labels": {"alertname": "NodeDown", "node": "lab10"}}
        self.assertEqual(wh.alert_topic(alert), "")

    def test_unrouted_host_uses_the_main_topic(self):
        alert = {"labels": {"alertname": "NodeDown", "node": "s1"}}
        self.assertEqual(wh.alert_topic(alert), "")

    def test_malformed_routes_are_ignored(self):
        os.environ["NTFY_LOCATIONS"] = "not json"
        alert = {"labels": {"alertname": "NodeDown", "node": "lab1"}}
        self.assertEqual(wh.alert_topic(alert), "")
        self.assertEqual(wh.host_topic_index(), {})

    def test_location_without_topic_or_hosts_is_ignored(self):
        os.environ["NTFY_LOCATIONS"] = '{"lab": {"hosts": ["lab1"]}}'
        self.assertEqual(wh.host_topic_index(), {})

    def test_send_posts_to_the_given_topic(self):
        with mock.patch.object(wh.requests, "post") as post:
            post.return_value.status_code = 200
            self.assertTrue(wh.send_to_ntfy("t", "m", topic="ids-topic"))
        self.assertTrue(post.call_args.args[0].endswith("/ids-topic"))

    def test_send_falls_back_to_the_main_topic(self):
        with mock.patch.object(wh.requests, "post") as post:
            post.return_value.status_code = 200
            self.assertTrue(wh.send_to_ntfy("t", "m"))
        self.assertTrue(post.call_args.args[0].endswith("/main-topic"))

    def test_missing_topic_drops_the_notification(self):
        for key in ("NTFY_TOPIC_MAIN", "NTFY_TOPIC_IDS"):
            os.environ.pop(key)
        with mock.patch.object(wh.requests, "post") as post:
            self.assertFalse(wh.send_to_ntfy("t", "m"))
        post.assert_not_called()

    def test_webhook_routes_each_alert_by_its_labels(self):
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "host-1",
                    "labels": {"alertname": "NodeDown", "severity": "critical", "node": "lab1"},
                    "annotations": {"summary": "lab1 down", "description": "down"},
                },
                {
                    "status": "firing",
                    "fingerprint": "ids-1",
                    "labels": {"alertname": "IDSAlert", "severity": "warning", "node": "s1"},
                    "annotations": {"summary": "ids", "description": "ids"},
                },
                {
                    "status": "firing",
                    "fingerprint": "main-1",
                    "labels": {"alertname": "NodeDown", "severity": "critical", "node": "s1"},
                    "annotations": {"summary": "s1 down", "description": "down"},
                },
            ]
        }
        with mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            post(payload)
        topics = [call.kwargs["topic"] for call in m.call_args_list]
        self.assertEqual(topics, ["lab-topic", "ids-topic", ""])


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

    def test_ids_budget_denials_do_not_consume_local_throttle(self):
        wh.IDS_RATE_LIMIT = 1
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh.THROTTLE_MAX_SENDS = 2
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"
        alerts = [
            {
                "status": "firing",
                "fingerprint": f"warn-{i}",
                "labels": {"alertname": "IDSAlert", "severity": "warning"},
                "annotations": {"summary": f"warn-{i}", "description": "warn"},
            }
            for i in range(3)
        ]
        alerts.append({
            "status": "firing",
            "fingerprint": "crit",
            "labels": {"alertname": "IDSAlert", "severity": "critical"},
            "annotations": {"summary": "crit", "description": "crit"},
        })
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post({"alerts": alerts})
        self.assertEqual(resp.get_json()["sent"], 3)
        self.assertEqual(resp.get_json()["throttled"], 2)
        self.assertEqual(resp.get_json()["cap_reached"], 1)
        self.assertEqual(m.call_count, 3)

    def test_ids_budget_skips_audit_rule(self):
        wh.IDS_RATE_LIMIT = 1
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "audit",
                    "labels": {"alertname": "IDSAlert", "severity": "warning", "rule_id": "container-api"},
                    "annotations": {"summary": "incus", "description": "incus"},
                },
                {
                    "status": "firing",
                    "fingerprint": "real",
                    "labels": {"alertname": "IDSAlert", "severity": "warning", "rule_id": "tcp-connect"},
                    "annotations": {"summary": "connect", "description": "connect"},
                },
            ]
        }
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post(payload)
        self.assertEqual(resp.get_json()["sent"], 2)
        self.assertEqual(resp.get_json()["throttled"], 0)

    def test_ids_budget_throttles_across_alerts(self):
        wh.IDS_RATE_LIMIT = 1
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": f"fp-{i}",
                    "labels": {"alertname": "IDSAlert", "severity": "warning"},
                    "annotations": {"summary": f"ids-{i}", "description": "ids"},
                }
                for i in range(2)
            ]
        }
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["sent"], 2)
        self.assertEqual(resp.get_json()["throttled"], 1)
        self.assertEqual(resp.get_json()["cap_reached"], 1)
        self.assertEqual(m.call_count, 2)
        cap_title, cap_message, cap_priority, cap_tags = m.call_args_list[1].args
        self.assertEqual(cap_title, "[WARNING] IDS alert budget reached")
        self.assertEqual(cap_priority, "default")
        self.assertIn("warning IDS alert budget", cap_message)
        self.assertIn("ids-1", cap_message)

    def test_ids_cap_notice_is_sent_once_per_window(self):
        wh.IDS_RATE_LIMIT = 1
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": f"fp-{i}",
                    "labels": {"alertname": "IDSAlert", "severity": "warning"},
                    "annotations": {"summary": f"ids-{i}", "description": "ids"},
                }
                for i in range(2)
            ]
        }
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True):
            post(payload)
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post({"alerts": [{
                "status": "firing",
                "fingerprint": "fp-2",
                "labels": {"alertname": "IDSAlert", "severity": "warning"},
                "annotations": {"summary": "ids-2", "description": "ids"},
            }]})
        self.assertEqual(resp.get_json()["sent"], 0)
        self.assertEqual(resp.get_json()["throttled"], 1)
        self.assertEqual(resp.get_json()["cap_reached"], 0)
        m.assert_not_called()

    def test_ids_cap_notice_uses_critical_priority(self):
        wh.IDS_RATE_LIMIT = 1
        wh.IDS_RATE_WINDOW_SECONDS = 3600
        wh._shared_client = FakeEtcd()
        os.environ["NTFY_SHARED_DEDUP"] = "1"
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": f"crit-{i}",
                    "labels": {"alertname": "IDSAlert", "severity": "critical"},
                    "annotations": {"summary": f"crit-{i}", "description": "crit"},
                }
                for i in range(2)
            ]
        }
        with unittest.mock.patch.object(wh, "send_to_ntfy", return_value=True) as m:
            resp = post(payload)
        self.assertEqual(resp.get_json()["cap_reached"], 1)
        cap_title, _, cap_priority, _ = m.call_args_list[1].args
        self.assertEqual(cap_title, "[CRITICAL] IDS alert budget reached")
        self.assertEqual(cap_priority, "urgent")

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
        wh._admit("firing:fp-1", now=wh_time.monotonic())
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
