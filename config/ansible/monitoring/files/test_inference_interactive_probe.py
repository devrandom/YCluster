#!/usr/bin/env python3
"""Unit tests for the interactive inference latency collector."""

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "inference_interactive_probe", _HERE / "inference-interactive-probe.py"
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def sample(ttft, e2e, prompt_tokens=64, completion_tokens=32):
    return {
        "ttft": ttft,
        "e2e": e2e,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": 0,
    }


class DiscoverTargetsTests(unittest.TestCase):
    def test_deduplicates_and_sorts_backend_model_pairs(self):
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"backend": "http://b:1", "model": "m2"}},
                    {"metric": {"backend": "http://a:1", "model": "m1"}},
                    {"metric": {"backend": "http://b:1", "model": "m2"}},
                    {"metric": {"backend": "", "model": "ignored"}},
                    {"metric": {"backend": "http://c:1"}},
                ]
            },
        }
        with mock.patch.object(probe, "_http_json", return_value=payload):
            self.assertEqual(
                probe.discover_targets("http://prom:9090"),
                [("http://a:1", "m1"), ("http://b:1", "m2")],
            )

    def test_rejects_prometheus_error_status(self):
        with mock.patch.object(probe, "_http_json", return_value={"status": "error"}):
            with self.assertRaises(RuntimeError):
                probe.discover_targets("http://prom:9090")


class BuildPromptTests(unittest.TestCase):
    def test_prompt_is_short_and_contains_nonce(self):
        prompt = probe.build_prompt(64, nonce="abc123")
        self.assertIn("abc123", prompt)
        self.assertLess(len(prompt.split()), 90)
        self.assertIn("three colors", prompt)


class ProbeTargetTests(unittest.TestCase):
    def test_warmup_is_excluded_and_p50_is_computed(self):
        timings = [sample(9.0, 9.0)] + [sample(0.1 * i, 0.2 * i) for i in range(1, 11)]
        with mock.patch.object(probe, "probe_once", side_effect=timings) as run:
            result = probe.probe_target("http://b:1", "m", requests=10)

        self.assertEqual(run.call_count, 11)
        self.assertTrue(result["warmup"])
        self.assertEqual(result["samples"], 10)
        self.assertAlmostEqual(result["ttft_p50"], 0.55)
        self.assertAlmostEqual(result["e2e_p50"], 1.1)
        self.assertEqual(result["prompt_tokens"], 64)
        self.assertEqual(result["completion_tokens"], 32)
        self.assertGreater(result["last_success"], 0)

    def test_warmup_failure_skips_measured_requests(self):
        with mock.patch.object(
            probe, "probe_once", side_effect=RuntimeError("connection refused")
        ) as run:
            result = probe.probe_target("http://b:1", "m", requests=10)

        self.assertEqual(run.call_count, 1)
        self.assertFalse(result["warmup"])
        self.assertEqual(result["samples"], 0)
        self.assertIn("connection refused", result["error"])

    def test_failed_measured_request_stops_the_series(self):
        with mock.patch.object(
            probe,
            "probe_once",
            side_effect=[sample(0.1, 0.2), sample(0.1, 0.2), RuntimeError("boom")],
        ) as run:
            result = probe.probe_target("http://b:1", "m", requests=10)

        self.assertEqual(run.call_count, 3)
        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["failures"], 1)
        self.assertIn("boom", result["error"])


class RenderTests(unittest.TestCase):
    def result(self, **overrides):
        result = {
            "backend": "http://b:1",
            "model": 'm"x',
            "samples": 10,
            "failures": 0,
            "warmup": True,
            "error": "",
            "ttft_p50": 0.393,
            "e2e_p50": 0.701,
            "prompt_tokens": 64,
            "completion_tokens": 32,
            "last_success": 100.0,
        }
        result.update(overrides)
        return result

    def test_renders_p50_metrics_and_escapes_labels(self):
        body = probe.render([self.result()], True, 1, 123.0)
        self.assertIn(
            "ycluster_inference_interactive_ttft_seconds{"
            'backend="http://b:1",model="m\\"x"} 0.393',
            body,
        )
        self.assertIn(
            "ycluster_inference_interactive_e2e_seconds{"
            'backend="http://b:1",model="m\\"x"} 0.701',
            body,
        )
        self.assertIn("ycluster_inference_interactive_probe_up{", body)
        self.assertIn("ycluster_inference_interactive_probe_discovery_up 1", body)

    def test_up_is_zero_when_not_all_requests_succeed(self):
        body = probe.render([self.result(samples=9)], True, 1, 123.0)
        self.assertIn(
            "ycluster_inference_interactive_probe_up{"
            'backend="http://b:1",model="m\\"x"} 0',
            body,
        )


class WriteOutputTests(unittest.TestCase):
    def test_output_is_world_readable_for_node_exporter(self):
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.prom"
            with mock.patch.object(probe, "OUT", out):
                probe.write_output("metric 1\n")
            self.assertEqual(out.read_text(), "metric 1\n")
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o644)


class MainTests(unittest.TestCase):
    def test_non_leader_removes_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.prom"
            out.write_text("stale\n")
            with (
                mock.patch.object(probe, "OUT", out),
                mock.patch.object(probe, "storage_leader", return_value="other"),
                mock.patch.object(probe, "short_hostname", return_value="self"),
                mock.patch.object(probe.sys, "stderr", new=io.StringIO()),
            ):
                self.assertEqual(probe.main(), 0)
            self.assertFalse(out.exists())

    def test_unknown_leader_keeps_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.prom"
            out.write_text("previous\n")
            with (
                mock.patch.object(probe, "OUT", out),
                mock.patch.object(probe, "storage_leader", return_value=None),
                mock.patch.object(probe.sys, "stderr", new=io.StringIO()),
            ):
                self.assertEqual(probe.main(), 1)
            self.assertEqual(out.read_text(), "previous\n")

    def test_leader_writes_discovered_metrics(self):
        result = {
            "backend": "http://b:1",
            "model": "m",
            "samples": 10,
            "failures": 0,
            "warmup": True,
            "error": "",
            "ttft_p50": 0.4,
            "e2e_p50": 0.7,
            "prompt_tokens": 64,
            "completion_tokens": 32,
            "last_success": 100.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.prom"
            with (
                mock.patch.object(probe, "OUT", out),
                mock.patch.object(probe, "storage_leader", return_value="self"),
                mock.patch.object(probe, "short_hostname", return_value="self"),
                mock.patch.object(
                    probe,
                    "discover_targets",
                    return_value=[("http://b:1", "m")],
                ),
                mock.patch.object(probe, "probe_target", return_value=result),
            ):
                self.assertEqual(probe.main(), 0)
            body = out.read_text()
            self.assertIn("ycluster_inference_interactive_e2e_seconds", body)
            self.assertIn('model="m"', body)


if __name__ == "__main__":
    unittest.main()
