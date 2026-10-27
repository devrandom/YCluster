#!/usr/bin/env python3
"""Publish IDS component health through the node_exporter textfile collector.

The Tetragon, CrowdSec and forwarder metrics endpoints are loopback-only by
design. Core Prometheus already scrapes node_exporter on every managed node,
so a small textfile keeps per-node coverage visible cluster-wide without
opening another listener.

The script is dependency-free and safe to run from a systemd oneshot unit.
"""

import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get("IDS_METRICS_DIR", "/var/lib/prometheus/node-exporter")
)
OUT = Path(os.environ.get("IDS_METRICS_OUT", TEXTFILE_DIR / "ids.prom"))

# component name -> loopback metrics URL
COMPONENTS = {
    "tetragon": "http://127.0.0.1:2112/metrics",
    "crowdsec": "http://127.0.0.1:6060/metrics",
    "ids-forwarder": "http://127.0.0.1:2113/metrics",
}

# Forwarder counters worth surfacing cluster-wide (the endpoint itself is
# loopback-only). Values are copied verbatim from its exposition format.
FORWARDER_GAUGES = (
    "ids_forwarder_cursor_errors_total",
    "ids_forwarder_dropped_total",
    "ids_forwarder_expired_total",
    "ids_forwarder_gap_total",
    "ids_forwarder_oversized_total",
    "ids_forwarder_queue_depth",
    "ids_forwarder_rejected_total",
    "ids_forwarder_spool_bytes",
    "ids_forwarder_suppressed_total",
)


def fetch(url: str, timeout: float = 2.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> None:
    lines = [
        "# HELP ids_component_up IDS component reachable on its loopback metrics endpoint.",
        "# TYPE ids_component_up gauge",
    ]
    bodies = {}
    for component, url in COMPONENTS.items():
        body = fetch(url)
        bodies[component] = body
        up = 1 if body is not None else 0
        lines.append(f'ids_component_up{{component="{component}"}} {up}')

    forwarder = bodies.get("ids-forwarder")
    if forwarder:
        for line in forwarder.splitlines():
            name = line.split("{", 1)[0].split(" ", 1)[0]
            if name in FORWARDER_GAUGES:
                lines.append(line)

    TEXTFILE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=TEXTFILE_DIR, prefix=".ids.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    main()
