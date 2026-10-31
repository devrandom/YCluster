#!/usr/bin/env python3
"""Unit tests for the IDS textfile collector."""

import importlib.util
import os
import runpy
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "ids_metrics_collector", _HERE / "ids-metrics-collector.py"
)
col = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(col)


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IdsMetricsCollectorTests(unittest.TestCase):
    def test_fetch_returns_body(self):
        with mock.patch.object(col.urllib.request, "urlopen",
                               return_value=_Response("a 1\n")):
            self.assertEqual(col.fetch("http://127.0.0.1:1/metrics"), "a 1\n")

    def test_fetch_rejects_error_status(self):
        with mock.patch.object(col.urllib.request, "urlopen",
                               return_value=_Response("", status=503)):
            self.assertIsNone(col.fetch("http://127.0.0.1:1/metrics"))

    def test_fetch_returns_none_on_transport_error(self):
        with mock.patch.object(col.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            self.assertIsNone(col.fetch("http://127.0.0.1:1/metrics"))

    def test_main_publishes_up_and_forwarder_gauges(self):
        forwarder = (
            "# HELP ids_forwarder_dropped_total dropped\n"
            "ids_forwarder_dropped_total 3\n"
            "ids_forwarder_queue_depth{queue=\"spool\"} 7\n"
            "unrelated_metric 9\n"
        )
        bodies = {
            "tetragon": "up 1\n",
            "crowdsec": None,
            "ids-forwarder": forwarder,
            "rsigma": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            original_dir, original_out = col.TEXTFILE_DIR, col.OUT
            col.TEXTFILE_DIR = Path(tmp) / "textfile"
            col.OUT = col.TEXTFILE_DIR / "ids.prom"
            try:
                with mock.patch.object(col, "fetch",
                                       side_effect=lambda url: bodies[
                                           next(name for name, u in
                                                col.COMPONENTS.items() if u == url)
                                       ]):
                    col.main()
                body = col.OUT.read_text()
            finally:
                col.TEXTFILE_DIR, col.OUT = original_dir, original_out

        self.assertIn('ids_component_up{component="tetragon"} 1', body)
        self.assertIn('ids_component_up{component="crowdsec"} 0', body)
        self.assertIn("ids_forwarder_dropped_total 3", body)
        self.assertIn('ids_forwarder_queue_depth{queue="spool"} 7', body)
        self.assertNotIn("unrelated_metric", body)
        self.assertFalse(list(col.OUT.parent.glob(".ids.*")))

    def test_main_skips_gauges_when_forwarder_is_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_dir, original_out = col.TEXTFILE_DIR, col.OUT
            col.TEXTFILE_DIR = Path(tmp)
            col.OUT = Path(tmp) / "ids.prom"
            try:
                with mock.patch.object(col, "fetch", return_value=None):
                    col.main()
                body = col.OUT.read_text()
            finally:
                col.TEXTFILE_DIR, col.OUT = original_dir, original_out

        self.assertNotIn("ids_forwarder_", body)
        self.assertIn('ids_component_up{component="rsigma"} 0', body)

    def test_main_removes_tempfile_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_dir, original_out = col.TEXTFILE_DIR, col.OUT
            col.TEXTFILE_DIR = Path(tmp)
            col.OUT = Path(tmp) / "ids.prom"
            try:
                with mock.patch.object(col, "fetch", return_value=None), \
                        mock.patch.object(col.os, "replace",
                                          side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        col.main()
                self.assertFalse(list(Path(tmp).glob(".ids.*")))
            finally:
                col.TEXTFILE_DIR, col.OUT = original_dir, original_out

    def test_module_entrypoint_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ids.prom"
            env = {"IDS_METRICS_OUT": str(out), "IDS_METRICS_DIR": tmp}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(col.urllib.request, "urlopen",
                                      side_effect=urllib.error.URLError("down")):
                runpy.run_path(str(_HERE / "ids-metrics-collector.py"),
                               run_name="__main__")
            self.assertIn('ids_component_up{component="tetragon"} 0',
                          out.read_text())


if __name__ == "__main__":
    unittest.main()
