#!/usr/bin/env python3
"""Emit hosting-provider account balances for node_exporter.

Provider-specific code is intentionally not part of the public repo.  At
runtime this collector loads every ``*.py`` plugin in a deployment-provided
plugin directory.  Each plugin must expose:

    PROVIDER = "provider-name"

    def fetch_balances() -> list[dict]:
        return [{"account": "default", "balance_dollars": 12.34}]

Plugins read their own credentials and convert provider-native units to
dollars.  A failed plugin produces a zero ``up`` gauge for that provider so
absent or broken plugins are observable; other plugins still contribute.
"""

import importlib.util
import math
import os
import sys
import tempfile
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get(
        "HOSTING_BALANCE_METRICS_DIR", "/var/lib/prometheus/node-exporter"
    )
)
OUT = Path(
    os.environ.get(
        "HOSTING_BALANCE_METRICS_OUT", TEXTFILE_DIR / "hosting_balance.prom"
    )
)
PLUGIN_DIR = Path(
    os.environ.get(
        "HOSTING_BALANCE_PLUGIN_DIR",
        "/usr/local/lib/cluster/hosting-balance-provider.d",
    )
)


def load_plugin(path):
    """Load and validate one provider plugin."""
    spec = importlib.util.spec_from_file_location(
        f"hosting_balance_provider_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin {path}")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    if not callable(getattr(plugin, "fetch_balances", None)):
        raise AttributeError(
            "hosting balance plugin does not define fetch_balances()"
        )
    provider = getattr(plugin, "PROVIDER", None)
    if not isinstance(provider, str) or not provider:
        raise AttributeError("hosting balance plugin does not define PROVIDER")
    return plugin


def label_value(value: str) -> str:
    """Escape a string for use as a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(results, errors) -> str:
    """Return the textfile body for a collection pass."""
    lines = [
        "# HELP ycluster_hosting_balance_dollars Current hosting account balance in dollars",
        "# TYPE ycluster_hosting_balance_dollars gauge",
    ]
    seen_accounts = set()
    for _name, provider, balances in results:
        for balance in balances:
            account = str(balance["account"])
            key = (provider, account)
            if key in seen_accounts:
                raise ValueError(f"duplicate provider/account {key}")
            seen_accounts.add(key)
            lines.append(
                f'ycluster_hosting_balance_dollars{{provider="{label_value(provider)}",'
                f'account="{label_value(account)}"}} {balance["balance_dollars"]}'
            )

    lines.extend(
        [
            "# HELP ycluster_hosting_balance_up Whether the latest collection for this account succeeded",
            "# TYPE ycluster_hosting_balance_up gauge",
        ]
    )
    for _name, provider, balances in results:
        for balance in balances:
            lines.append(
                f'ycluster_hosting_balance_up{{provider="{label_value(provider)}",'
                f'account="{label_value(balance["account"])}"}} 1'
            )
    for provider, account in sorted(errors):
        lines.append(
            f'ycluster_hosting_balance_up{{provider="{label_value(provider)}",'
            f'account="{label_value(account)}"}} 0'
        )
    return "\n".join(lines) + "\n"


def write_output(text: str) -> None:
    """Atomically replace the textfile so node_exporter never sees a partial write."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".hosting_balance.", dir=OUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, OUT)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def collect() -> str:
    """Collect every plugin, retaining errors as zero-up account series."""
    results = []
    errors = set()
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        try:
            plugin = load_plugin(path)
        except Exception as exc:
            errors.add((path.stem, "default"))
            print(f"hosting-balance: {path.stem} failed: {exc}", file=sys.stderr)
            continue

        provider = plugin.PROVIDER
        try:
            balances = []
            for raw in plugin.fetch_balances():
                account = str(raw.get("account", "default"))
                balance = float(raw["balance_dollars"])
                if not math.isfinite(balance) or balance < 0:
                    raise ValueError(
                        f"invalid balance for {provider}/{account}: {balance}"
                    )
                balances.append(
                    {
                        "account": account,
                        "balance_dollars": balance,
                    }
                )
            if not balances:
                raise ValueError(f"plugin returned no accounts for {provider}")
            results.append((path.stem, provider, balances))
        except Exception as exc:
            errors.add((provider, "default"))
            print(f"hosting-balance: {path.stem} failed: {exc}", file=sys.stderr)

    if not results and not errors:
        errors.add(("unconfigured", "default"))
        print(
            f"hosting-balance: no plugins found in {PLUGIN_DIR}", file=sys.stderr
        )
    return render(results, errors)


def main() -> int:
    failed = False
    try:
        text = collect()
    except Exception as exc:
        text = render([], {("unconfigured", "default")})
        failed = True
        print(f"hosting-balance: collection failed: {exc}", file=sys.stderr)

    write_output(text)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
