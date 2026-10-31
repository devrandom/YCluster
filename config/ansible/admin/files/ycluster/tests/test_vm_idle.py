"""Tests for the idle-GPU detector and its alert bookkeeping."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ycluster.utils import vm_idle

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def points_for(values, now=NOW, step=60):
    """One point per minute, ending at `now`."""
    start = now - timedelta(minutes=len(values))
    return [(start + timedelta(minutes=i), v)
            for i, v in enumerate(values)]


def day(util=1.0):
    return [util] * 1440


class EvaluateTest(unittest.TestCase):
    def test_idle_after_a_quiet_day(self):
        result = vm_idle.evaluate(points_for(day()), now=NOW)
        self.assertEqual(result["status"], "idle")
        self.assertTrue(result["alert"])
        self.assertTrue(result["covered"])
        self.assertEqual(result["high_samples"], 0)
        self.assertEqual(result["low_samples"], 0)

    def test_active_on_ten_high_samples(self):
        values = day()
        values[100:110] = [50.0] * 10
        result = vm_idle.evaluate(points_for(values), now=NOW)
        self.assertEqual(result["high_samples"], 10)
        self.assertEqual(result["status"], "in use")
        self.assertFalse(result["alert"])

    def test_active_on_twenty_low_samples(self):
        values = day()
        values[100:120] = [10.0] * 20
        result = vm_idle.evaluate(points_for(values), now=NOW)
        self.assertEqual(result["high_samples"], 0)
        self.assertEqual(result["low_samples"], 20)
        self.assertEqual(result["status"], "in use")

    def test_just_below_both_thresholds_is_idle(self):
        values = day()
        values[100:109] = [50.0] * 9
        values[200:210] = [10.0] * 10
        result = vm_idle.evaluate(points_for(values), now=NOW)
        self.assertEqual(result["high_samples"], 9)
        self.assertEqual(result["low_samples"], 19)
        self.assertEqual(result["status"], "idle")

    def test_exactly_at_high_threshold_does_not_count(self):
        values = day()
        values[100:110] = [20.0] * 10
        result = vm_idle.evaluate(points_for(values), now=NOW)
        self.assertEqual(result["high_samples"], 0)
        self.assertEqual(result["status"], "idle")

    def test_exactly_at_low_threshold_does_not_count(self):
        values = day()
        values[100:120] = [5.0] * 20
        result = vm_idle.evaluate(points_for(values), now=NOW)
        self.assertEqual(result["low_samples"], 0)
        self.assertEqual(result["status"], "idle")

    def test_short_history_is_insufficient(self):
        result = vm_idle.evaluate(points_for([1.0] * 600), now=NOW)
        self.assertEqual(result["status"], "insufficient data")
        self.assertFalse(result["alert"])

    def test_gap_is_insufficient(self):
        points = points_for(day())
        del points[700:720]
        result = vm_idle.evaluate(points, now=NOW)
        self.assertEqual(result["status"], "insufficient data")
        self.assertFalse(result["alert"])

    def test_recently_stopped_is_insufficient(self):
        # Series covers the start of the window but ends two hours ago: the VM
        # was stopped, so there is nothing to remind its owner about.
        result = vm_idle.evaluate(points_for(day())[:-120], now=NOW)
        self.assertEqual(result["status"], "insufficient data")
        self.assertFalse(result["alert"])

    def test_small_scrape_lag_still_counts(self):
        result = vm_idle.evaluate(points_for(day())[:-4], now=NOW)
        self.assertEqual(result["status"], "idle")
        self.assertTrue(result["alert"])

    def test_no_points_is_no_data(self):
        result = vm_idle.evaluate([], now=NOW)
        self.assertEqual(result["status"], "no GPU data")
        self.assertFalse(result["alert"])

    def test_points_outside_window_are_ignored(self):
        points = points_for([50.0] * 100, now=NOW - timedelta(days=2))
        result = vm_idle.evaluate(points, now=NOW)
        self.assertEqual(result["status"], "no GPU data")

    def test_max_utilization(self):
        result = vm_idle.evaluate(points_for(day(3.0)), now=NOW)
        self.assertAlmostEqual(result["max_util"], 3.0)


class FetchGpuSeriesTest(unittest.TestCase):
    def test_parses_series_and_skips_non_finite(self):
        payload = {"status": "success", "data": {"result": [
            {"metric": {"vm": "vm1"},
             "values": [[100, "0"], [160, "25.5"], [220, "NaN"]]},
            {"metric": {"vm": "vm2"}, "values": [[100, "3"]]},
            {"metric": {}, "values": [[100, "99"]]},
        ]}}
        response = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(vm_idle.requests, "get",
                               return_value=response) as get:
            series = vm_idle.fetch_gpu_series(now=NOW, url="http://prom")
        self.assertEqual(set(series), {"vm1", "vm2"})
        self.assertEqual(len(series["vm1"]), 2)
        self.assertEqual(series["vm1"][1][1], 25.5)
        self.assertEqual(series["vm2"][0][1], 3.0)
        self.assertEqual(get.call_args.args[0],
                         "http://prom/api/v1/query_range")
        self.assertEqual(get.call_args.kwargs["params"]["step"], 60)

    def test_http_error_raises(self):
        response = mock.Mock()
        response.raise_for_status.side_effect = vm_idle.requests.HTTPError("502")
        with mock.patch.object(vm_idle.requests, "get",
                               return_value=response):
            with self.assertRaises(RuntimeError):
                vm_idle.fetch_gpu_series(now=NOW)

    def test_query_error_raises(self):
        response = mock.Mock()
        response.json.return_value = {"status": "error", "error": "bad query"}
        with mock.patch.object(vm_idle.requests, "get",
                               return_value=response):
            with self.assertRaises(RuntimeError):
                vm_idle.fetch_gpu_series(now=NOW)


class ReportTest(unittest.TestCase):
    def _report(self, vms, series):
        with mock.patch.object(vm_idle.vm, "vms_all", return_value=vms), \
                mock.patch.object(vm_idle, "fetch_gpu_series",
                                  return_value=series):
            return {r["name"]: r for r in vm_idle.report(now=NOW)}

    def test_no_gpu_never_alerts(self):
        results = self._report(
            {"cpu-only": {"owner": "a", "host": "h", "gpus": 0}},
            {"cpu-only": points_for(day())})
        self.assertEqual(results["cpu-only"]["status"], "no GPU")
        self.assertFalse(results["cpu-only"]["alert"])

    def test_missing_gpu_metric_never_alerts(self):
        results = self._report(
            {"gpu": {"owner": "a", "host": "h", "gpus": 1}}, {})
        self.assertEqual(results["gpu"]["status"], "no GPU data")
        self.assertFalse(results["gpu"]["alert"])

    def test_idle_gpu_alerts(self):
        results = self._report(
            {"gpu": {"owner": "a", "host": "h", "gpus": 1}},
            {"gpu": points_for(day())})
        self.assertEqual(results["gpu"]["status"], "idle")
        self.assertTrue(results["gpu"]["alert"])

    def test_busy_gpu_does_not_alert(self):
        results = self._report(
            {"gpu": {"owner": "a", "host": "h", "gpus": 1}},
            {"gpu": points_for(day(40.0))})
        self.assertEqual(results["gpu"]["status"], "in use")
        self.assertFalse(results["gpu"]["alert"])

    def test_unregistered_series_are_ignored(self):
        results = self._report(
            {"known": {"owner": "a", "host": "h", "gpus": 1}},
            {"unknown": points_for(day())})
        self.assertEqual(set(results), {"known"})


class FormatReportTest(unittest.TestCase):
    def test_renders_status_and_counts(self):
        results = [
            dict(vm_idle.evaluate(points_for(day()), now=NOW),
                 name="quiet", owner="owner-a"),
            dict(vm_idle.evaluate(points_for(day(50.0)), now=NOW),
                 name="busy", owner="owner-b"),
        ]
        text = vm_idle.format_report(results)
        self.assertIn("quiet", text)
        self.assertIn("idle", text)
        self.assertIn("busy", text)
        self.assertIn("in use", text)
        self.assertIn("50%", text)
        self.assertLess(text.index("quiet"), text.index("busy"))


class NotifyTest(unittest.TestCase):
    def setUp(self):
        # notify() consults the etcd suppression list before every run.
        patcher = mock.patch.object(vm_idle, "suppressed_owners",
                                   return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _results(self, status):
        return [{"name": "vm1", "owner": "owner@example.test",
                 "status": status, "alert": status == "idle",
                 "samples": 1440, "high_samples": 0, "low_samples": 0}]

    def test_alerts_once_per_episode(self):
        settings = ("bot@example.test", "key", "example.test")
        marker = {}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings", return_value=settings), \
                mock.patch.object(vm_idle, "send_zulip",
                                  return_value="direct") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda key: marker.get(key)), \
                mock.patch.object(vm_idle.vm, "_put_json",
                                  side_effect=lambda key, obj: marker.update({key: obj})), \
                mock.patch.object(vm_idle.vm, "_delete") as delete:
            self.assertEqual(
                vm_idle.notify(self._results("idle"), now=NOW), 1)
            self.assertEqual(
                vm_idle.notify(self._results("idle"), now=NOW), 0)
            send.assert_called_once()
            delete.assert_not_called()

    def test_realerts_after_a_day(self):
        settings = ("bot@example.test", "key", "example.test")
        key = vm_idle._alert_key("vm1")
        marker = {key: {"alerted_at": (NOW - timedelta(days=1, hours=1)).isoformat()}}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings", return_value=settings), \
                mock.patch.object(vm_idle, "send_zulip",
                                  return_value="direct") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda k: marker.get(k)), \
                mock.patch.object(vm_idle.vm, "_put_json",
                                  side_effect=lambda key, obj: marker.update({key: obj})):
            self.assertEqual(vm_idle.notify(self._results("idle"), now=NOW), 1)
            send.assert_called_once()

    def test_no_realert_within_a_day(self):
        settings = ("bot@example.test", "key", "example.test")
        key = vm_idle._alert_key("vm1")
        marker = {key: {"alerted_at": (NOW - timedelta(hours=23)).isoformat()}}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings", return_value=settings), \
                mock.patch.object(vm_idle, "send_zulip") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda k: marker.get(k)):
            self.assertEqual(vm_idle.notify(self._results("idle"), now=NOW), 0)
            send.assert_not_called()

    def test_in_use_clears_marker(self):
        key = vm_idle._alert_key("vm1")
        marker = {key: {"alerted_at": NOW.isoformat()}}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "send_zulip") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda k: marker.get(k)), \
                mock.patch.object(vm_idle.vm, "_delete",
                                  side_effect=lambda k: marker.pop(k, None)) as delete:
            self.assertEqual(vm_idle.notify(self._results("in use"),
                                           now=NOW), 0)
            delete.assert_called_once_with(key)
            send.assert_not_called()

    def test_no_gpu_clears_marker(self):
        key = vm_idle._alert_key("vm1")
        marker = {key: {"alerted_at": NOW.isoformat()}}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "send_zulip") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda k: marker.get(k)), \
                mock.patch.object(vm_idle.vm, "_delete",
                                  side_effect=lambda k: marker.pop(k, None)) as delete:
            self.assertEqual(vm_idle.notify(self._results("no GPU data"),
                                           now=NOW), 0)
            delete.assert_called_once_with(key)
            send.assert_not_called()

    def test_insufficient_data_keeps_marker(self):
        key = vm_idle._alert_key("vm1")
        marker = {key: {"alerted_at": NOW.isoformat()}}
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "send_zulip") as send, \
                mock.patch.object(vm_idle.vm, "_get_json",
                                  side_effect=lambda k: marker.get(k)), \
                mock.patch.object(vm_idle.vm, "_delete") as delete:
            self.assertEqual(vm_idle.notify(self._results("insufficient data"),
                                           now=NOW), 0)
            delete.assert_not_called()
            send.assert_not_called()

    def test_failed_delivery_retries_next_run(self):
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "send_zulip",
                                  return_value="failed"), \
                mock.patch.object(vm_idle.vm, "_get_json", return_value=None), \
                mock.patch.object(vm_idle.vm, "_put_json") as put:
            self.assertEqual(vm_idle.notify(self._results("idle"), now=NOW), 0)
            put.assert_not_called()

    def test_missing_credentials_raises(self):
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings", return_value=None):
            with self.assertRaises(RuntimeError):
                vm_idle.notify(self._results("idle"), now=NOW)


class SendZulipTest(unittest.TestCase):
    def _result(self):
        return {"name": "vm1", "owner": "owner@example.test", "status": "idle",
                "samples": 1440, "high_samples": 0, "low_samples": 0}

    def _response(self, ok):
        response = mock.Mock()
        response.ok = ok
        return response

    def test_direct_uses_form_encoding(self):
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post",
                                  return_value=self._response(True)) as post:
            outcome = vm_idle.send_zulip(("bot@realm.test", "key", "realm.test"),
                                        self._result())
        self.assertEqual(outcome, "direct")
        payload = post.call_args.kwargs["data"]
        self.assertNotIn("json", post.call_args.kwargs)
        self.assertEqual(payload["type"], "direct")
        self.assertEqual(json.loads(payload["to"]), ["user7@realm.test"])
        self.assertIn("vm1", payload["content"])

    def test_falls_back_to_stream(self):
        responses = [self._response(False), self._response(True)]
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post",
                                  side_effect=responses) as post:
            outcome = vm_idle.send_zulip(("bot@realm.test", "key", "realm.test"),
                                        self._result(), stream="ops")
        self.assertEqual(outcome, "stream")
        payload = post.call_args.kwargs["data"]
        self.assertEqual(payload["type"], "stream")
        self.assertEqual(payload["to"], "ops")
        self.assertEqual(payload["topic"], "idle VMs")

    def test_direct_failure_without_stream_is_failed(self):
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post",
                                  return_value=self._response(False)) as post:
            outcome = vm_idle.send_zulip(("bot@realm.test", "key", "realm.test"),
                                        self._result())
        self.assertEqual(outcome, "failed")
        self.assertEqual(post.call_count, 1)

    def test_network_error_without_stream_is_failed(self):
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post",
                                  side_effect=vm_idle.requests.ConnectionError("x")):
            outcome = vm_idle.send_zulip(("bot@realm.test", "key", "realm.test"),
                                        self._result())
        self.assertEqual(outcome, "failed")


class MessageTest(unittest.TestCase):
    def test_points_at_web_ui_not_the_admin_cli(self):
        result = dict(vm_idle.evaluate(points_for(day()), now=NOW),
                      name="vm1", owner="owner@example.test")
        text = vm_idle._message(result, vm_idle.IDLE_AFTER,
                               vm_page="https://admin.example.test/admin/vm-schedule")
        self.assertIn("vm1", text)
        self.assertIn("owner@example.test", text)
        self.assertIn("GPU", text)
        self.assertIn("24h", text)
        self.assertIn("https://admin.example.test/admin/vm-schedule", text)
        self.assertIn("Stop now", text)
        self.assertNotIn("ycluster vm stop", text)

    def test_alert_url_is_built_from_the_domain(self):
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post") as post:
            post.return_value.ok = True
            vm_idle.send_zulip(("bot@realm.test", "key", "realm.test"),
                               SendZulipTest()._result())
        content = post.call_args.kwargs["data"]["content"]
        self.assertIn("https://admin.realm.test/admin/vm-schedule", content)


class SuppressionTest(unittest.TestCase):
    def test_set_and_read_suppressed_owners(self):
        store = {}

        class Client:
            def get(self, key):
                return store.get(key), None

            def put(self, key, value):
                store[key] = value.encode()

        with mock.patch.object(vm_idle, "get_etcd_client", return_value=Client()):
            self.assertEqual(vm_idle.suppressed_owners(), set())
            self.assertEqual(
                vm_idle.set_suppressed(" Owner@Example.test ", True),
                ["owner@example.test"])
            self.assertEqual(vm_idle.suppressed_owners(), {"owner@example.test"})
            self.assertEqual(
                vm_idle.set_suppressed("other@example.test", True),
                ["other@example.test", "owner@example.test"])
            self.assertEqual(
                vm_idle.set_suppressed("owner@example.test", False),
                ["other@example.test"])

    def test_bad_value_reads_as_empty(self):
        class Client:
            def get(self, key):
                return b"not json", None

        with mock.patch.object(vm_idle, "get_etcd_client", return_value=Client()):
            self.assertEqual(vm_idle.suppressed_owners(), set())

    def test_muted_owner_is_skipped(self):
        results = [{"name": "vm1", "owner": "muted@example.test",
                    "status": "idle", "alert": True, "samples": 1440,
                    "high_samples": 0, "low_samples": 0}]
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "suppressed_owners",
                               return_value={"muted@example.test"}), \
                mock.patch.object(vm_idle, "send_zulip") as send:
            self.assertEqual(vm_idle.notify(results, now=NOW), 0)
        send.assert_not_called()

    def test_mute_is_case_insensitive(self):
        results = [{"name": "vm1", "owner": "Muted@Example.Test",
                    "status": "idle", "alert": True, "samples": 1440,
                    "high_samples": 0, "low_samples": 0}]
        with mock.patch("builtins.print"), \
                mock.patch.object(vm_idle, "_zulip_settings",
                               return_value=("b", "k", "d")), \
                mock.patch.object(vm_idle, "suppressed_owners",
                               return_value={"muted@example.test"}), \
                mock.patch.object(vm_idle, "send_zulip") as send:
            self.assertEqual(vm_idle.notify(results, now=NOW), 0)
        send.assert_not_called()


class BookkeepingEdgeTests(unittest.TestCase):
    def test_non_numeric_point_is_skipped(self):
        payload = {"status": "success", "data": {"result": [
            {"metric": {"vm": "vm1"},
             "values": [[100, "not-a-number"], [160, "1"]]},
        ]}}
        response = mock.Mock()
        response.json.return_value = payload
        with mock.patch.object(vm_idle.requests, "get",
                               return_value=response):
            series = vm_idle.fetch_gpu_series(now=NOW, url="http://prom")
        self.assertEqual(len(series["vm1"]), 1)

    def test_unparseable_alert_timestamp_is_due(self):
        with mock.patch.object(vm_idle.vm, "_get_json",
                               return_value={"alerted_at": "garbage"}):
            self.assertTrue(vm_idle._alert_due("vm1", NOW))

    def test_suppression_list_must_be_a_list(self):
        client = mock.Mock()
        client.get.return_value = (json.dumps({"owner": True}).encode(), None)
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client):
            self.assertEqual(vm_idle.suppressed_owners(), set())

    def test_suppression_requires_owner(self):
        with mock.patch.object(vm_idle, "suppressed_owners",
                               return_value=set()):
            with self.assertRaises(ValueError):
                vm_idle.set_suppressed("  ", True)

    def test_zulip_settings_requires_all_keys(self):
        client = mock.Mock()
        client.get.side_effect = lambda key: (
            b"bot@example.test" if key == vm_idle.ZULIP_BOT_EMAIL_KEY
            else (b"key" if key == vm_idle.ZULIP_BOT_API_KEY else None), None)
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client):
            self.assertIsNone(vm_idle._zulip_settings())

    def test_zulip_settings_returns_credentials(self):
        client = mock.Mock()
        client.get.return_value = (b"value", None)
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client):
            self.assertEqual(vm_idle._zulip_settings(),
                             ("value", "value", "value"))

    def test_resolve_recipient_without_owner(self):
        self.assertIsNone(vm_idle.resolve_recipient(None))

    def test_resolve_recipient_without_password(self):
        client = mock.Mock()
        client.get.return_value = (None, None)
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client):
            self.assertEqual(vm_idle.resolve_recipient("a@example.test"),
                             "a@example.test")

    def test_resolve_recipient_maps_realm_address(self):
        import sys
        import types
        client = mock.Mock()
        client.get.return_value = (b"pw", None)
        cursor = mock.MagicMock()
        cursor.__enter__.return_value.fetchone.return_value = ("user7@realm",)
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        fake = types.ModuleType("psycopg2")
        fake.connect = mock.Mock(return_value=conn)
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client), \
                mock.patch.dict(sys.modules, {"psycopg2": fake}):
            self.assertEqual(vm_idle.resolve_recipient("a@example.test"),
                             "user7@realm")
        conn.close.assert_called_once()

    def test_resolve_recipient_falls_back_on_error(self):
        import sys
        import types
        client = mock.Mock()
        client.get.return_value = (b"pw", None)
        fake = types.ModuleType("psycopg2")
        fake.connect = mock.Mock(side_effect=RuntimeError("down"))
        with mock.patch.object(vm_idle, "get_etcd_client",
                               return_value=client), \
                mock.patch.dict(sys.modules, {"psycopg2": fake}), \
                mock.patch("builtins.print"):
            self.assertEqual(vm_idle.resolve_recipient("a@example.test"),
                             "a@example.test")

    def test_stream_retry_network_error_is_failed(self):
        response = mock.Mock()
        response.ok = False
        with mock.patch.object(vm_idle, "resolve_recipient",
                               return_value="user7@realm.test"), \
                mock.patch.object(vm_idle.requests, "post",
                                  side_effect=[response,
                                               vm_idle.requests.RequestException("down")]):
            outcome = vm_idle.send_zulip(
                ("bot@realm.test", "key", "realm.test"),
                {"name": "vm1", "owner": "a@example.test",
                 "status": "idle", "samples": 1440,
                 "high_samples": 0, "low_samples": 0},
                stream="ops")
        self.assertEqual(outcome, "failed")


if __name__ == "__main__":
    unittest.main()
