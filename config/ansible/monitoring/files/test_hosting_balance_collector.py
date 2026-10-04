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


if __name__ == "__main__":
    unittest.main()
