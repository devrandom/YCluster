#!/usr/bin/env python3
"""Unit tests for the generic hosting-balance textfile collector."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "hosting_balance_collector", _HERE / "hosting-balance-collector.py"
)
col = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(col)


def plugin_source(provider, balances):
    return f'PROVIDER = "{provider}"\n\ndef fetch_balances():\n    return {balances}\n'


class HostingBalanceCollectorTests(unittest.TestCase):
    def test_render_includes_provider_account_and_up(self):
        body = col.render(
            [
                (
                    "plugin",
                    "provider",
                    [{"account": "primary", "balance_dollars": 98.613}],
                )
            ],
            set(),
        )
        self.assertIn(
            'ycluster_hosting_balance_dollars{provider="provider",'
            'account="primary"} 98.613',
            body,
        )
        self.assertIn(
            'ycluster_hosting_balance_up{provider="provider",'
            'account="primary"} 1',
            body,
        )

    def test_collect_loads_multiple_plugins_and_accounts(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            (Path(plugin_dir) / "one.py").write_text(
                plugin_source(
                    "one", '[{"account": "a", "balance_dollars": 1.0}]'
                )
            )
            (Path(plugin_dir) / "two.py").write_text(
                plugin_source(
                    "two",
                    '[{"account": "a", "balance_dollars": 2.0},'
                    ' {"account": "b", "balance_dollars": 3.0}]',
                )
            )
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn('provider="one",account="a"} 1.0', body)
        self.assertIn('provider="two",account="a"} 2.0', body)
        self.assertIn('provider="two",account="b"} 3.0', body)
        self.assertEqual(body.count("} 1\n"), 3)

    def test_collect_continues_after_plugin_error(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            (Path(plugin_dir) / "broken.py").write_text(
                plugin_source("broken", '1 / 0')
            )
            (Path(plugin_dir) / "working.py").write_text(
                plugin_source(
                    "working", '[{"balance_dollars": 4.0}]'
                )
            )
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                with mock.patch.object(col.sys, "stderr"):
                    body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn(
            'ycluster_hosting_balance_up{provider="broken",account="default"} 0',
            body,
        )
        self.assertIn(
            'ycluster_hosting_balance_dollars{provider="working",'
            'account="default"} 4.0',
            body,
        )

    def test_collect_reports_unconfigured(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                with mock.patch.object(col.sys, "stderr"):
                    body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn(
            'ycluster_hosting_balance_up{provider="unconfigured",'
            'account="default"} 0',
            body,
        )

    def test_main_writes_output_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as out_dir:
            out_path = Path(out_dir) / "hosting_balance.prom"
            original_out = col.OUT
            col.OUT = out_path
            try:
                with mock.patch.object(
                    col,
                    "collect",
                    return_value='ycluster_hosting_balance_up 1\n',
                ):
                    self.assertEqual(col.main(), 0)
                self.assertIn("ycluster_hosting_balance_up 1", out_path.read_text())
            finally:
                col.OUT = original_out


    def test_load_plugin_rejects_missing_spec(self):
        with mock.patch.object(col.importlib.util, "spec_from_file_location",
                               return_value=None):
            with self.assertRaises(ImportError):
                col.load_plugin(Path("plugin.py"))

    def test_load_plugin_requires_fetch_balances(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            path = Path(plugin_dir) / "no_fetch.py"
            path.write_text('PROVIDER = "none"\n')
            with self.assertRaises(AttributeError):
                col.load_plugin(path)

    def test_load_plugin_requires_provider(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            path = Path(plugin_dir) / "no_provider.py"
            path.write_text('def fetch_balances():\n    return []\n')
            with self.assertRaises(AttributeError):
                col.load_plugin(path)

    def test_render_rejects_duplicate_account(self):
        results = [
            ("one", "provider", [{"account": "a", "balance_dollars": 1.0}]),
            ("two", "provider", [{"account": "a", "balance_dollars": 2.0}]),
        ]
        with self.assertRaises(ValueError):
            col.render(results, set())

    def test_write_output_removes_partial_file_on_failure(self):
        with tempfile.TemporaryDirectory() as out_dir:
            original_out = col.OUT
            col.OUT = Path(out_dir) / "hosting_balance.prom"
            try:
                with mock.patch.object(col.os, "replace",
                                       side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        col.write_output("body\n")
                self.assertFalse(list(Path(out_dir).glob(".hosting_balance.*")))
            finally:
                col.OUT = original_out

    def test_write_output_tolerates_already_removed_tempfile(self):
        with tempfile.TemporaryDirectory() as out_dir:
            original_out = col.OUT
            col.OUT = Path(out_dir) / "hosting_balance.prom"
            try:
                with mock.patch.object(col.os, "replace",
                                       side_effect=OSError("disk full")), \
                        mock.patch.object(col.os, "unlink",
                                          side_effect=FileNotFoundError):
                    with self.assertRaises(OSError):
                        col.write_output("body\n")
            finally:
                col.OUT = original_out

    def test_collect_skips_unloadable_plugin(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            (Path(plugin_dir) / "broken.py").write_text("def fetch_balances(:\n")
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                with mock.patch.object(col.sys, "stderr"):
                    body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn(
            'ycluster_hosting_balance_up{provider="broken",account="default"} 0',
            body,
        )

    def test_collect_reports_invalid_balance(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            (Path(plugin_dir) / "negative.py").write_text(
                plugin_source("negative", '[{"balance_dollars": -1}]')
            )
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                with mock.patch.object(col.sys, "stderr"):
                    body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn(
            'ycluster_hosting_balance_up{provider="negative",account="default"} 0',
            body,
        )

    def test_collect_reports_empty_plugin(self):
        with tempfile.TemporaryDirectory() as plugin_dir:
            (Path(plugin_dir) / "empty.py").write_text(
                plugin_source("empty", '[]')
            )
            original = col.PLUGIN_DIR
            col.PLUGIN_DIR = Path(plugin_dir)
            try:
                with mock.patch.object(col.sys, "stderr"):
                    body = col.collect()
            finally:
                col.PLUGIN_DIR = original

        self.assertIn(
            'ycluster_hosting_balance_up{provider="empty",account="default"} 0',
            body,
        )

    def test_main_reports_collection_failure(self):
        with tempfile.TemporaryDirectory() as out_dir:
            original_out = col.OUT
            col.OUT = Path(out_dir) / "hosting_balance.prom"
            try:
                with mock.patch.object(col, "collect",
                                       side_effect=RuntimeError("boom")), \
                        mock.patch.object(col.sys, "stderr"):
                    self.assertEqual(col.main(), 1)
                self.assertIn(
                    'provider="unconfigured"', col.OUT.read_text())
            finally:
                col.OUT = original_out

    def test_module_entrypoint_runs_main(self):
        import os
        import runpy
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugins"
            plugin_dir.mkdir()
            (plugin_dir / "one.py").write_text(
                plugin_source("one", '[{"balance_dollars": 5}]')
            )
            out = Path(tmp) / "out.prom"
            env = {
                "HOSTING_BALANCE_PLUGIN_DIR": str(plugin_dir),
                "HOSTING_BALANCE_METRICS_OUT": str(out),
            }
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(SystemExit) as exit_info:
                    runpy.run_path(str(_HERE / "hosting-balance-collector.py"),
                                   run_name="__main__")
            self.assertEqual(exit_info.exception.code, 0)
            self.assertIn('provider="one"', out.read_text())


if __name__ == "__main__":
    unittest.main()
