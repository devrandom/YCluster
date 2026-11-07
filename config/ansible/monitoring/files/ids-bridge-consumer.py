#!/usr/bin/env python3
"""Forward VM bridge guard blocks to the ids-forwarder.

The bridge guard (admin/install-vm-bridge-guard.yml) is a traffic filter, not
an IDS component; it logs one kernel line per blocked frame, rate limited on
the host. This consumer is the IDS side of that interface. Those lines carry guest addresses, so they stay on the
host: this consumer reduces each line to its class and posts a
CrowdSec-shaped alert to the forwarder's loopback receiver, which is the
same path CrowdSec's notification-http plugin uses. The alert message is
static, so no address, port or interface name reaches Alertmanager.

Only the first event of each class within the budget window is posted. The
exact count stays on the host in the nftables counter
(`nft list table bridge vm_bridge_guard`); a per-packet alert would burn
the cluster-wide notification budget on a single scan.

Dependency-free and safe to run from a long-lived systemd unit.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOG_PREFIX = "vm-bridge-guard "

# Guard class -> (scenario, alert message). The message is deliberately
# static: it is what Alertmanager stores and notifies.
CLASSES = {
    "neighbour": (
        "vm-bridge-neighbour-egress",
        "guest opened a connection to another guest on the VM bridge",
    ),
    "cluster": (
        "vm-bridge-cluster-egress",
        "guest tried to reach a cluster network through the VM bridge",
    ),
}

DEFAULT_RECEIVER = "http://127.0.0.1:9201/crowdsec"


def classify(line):
    """Return the guard class named in a kernel log line, or None."""
    idx = line.find(LOG_PREFIX)
    if idx < 0:
        return None
    rest = line[idx + len(LOG_PREFIX):].lstrip()
    return rest.split(" ", 1)[0] or None


class RateLimiter:
    """Allow at most `burst` events per `window` seconds."""

    def __init__(self, burst=1, window=3600.0, clock=time.monotonic):
        self._burst = burst
        self._window = window
        self._clock = clock
        self._events = []

    def allow(self):
        now = self._clock()
        self._events = [t for t in self._events if now - t < self._window]
        if len(self._events) >= self._burst:
            return False
        self._events.append(now)
        return True


def payload(cls, hostname):
    """Build the CrowdSec notification-http body for one guard class."""
    scenario, message = CLASSES[cls]
    return [
        {
            "scenario": scenario,
            "machine": {"hostname": hostname},
            "events": [{"message": message}],
        }
    ]


def post(receiver, body, timeout=5.0):
    request = urllib.request.Request(
        receiver,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def handle_line(line, receiver, hostname, limiters, post_fn=None):
    """Post one alert for a guard line. Return True when an alert was sent.

    `limiters` maps each class to its own RateLimiter, so a neighbour alert
    does not consume the cluster class's budget.
    """
    cls = classify(line)
    if cls not in CLASSES:
        return False
    if not limiters[cls].allow():
        return False
    try:
        (post_fn or post)(receiver, payload(cls, hostname))
    except (urllib.error.URLError, OSError) as exc:
        print("ids-bridge-consumer: post failed: {}".format(exc), file=sys.stderr)
        return False
    return True


def journal_lines():
    proc = subprocess.Popen(
        ["journalctl", "-k", "-f", "-n", "0", "-o", "cat"],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            yield line
    finally:
        proc.terminate()
        proc.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receiver", default=DEFAULT_RECEIVER)
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--burst", type=int, default=1)
    parser.add_argument("--window", type=float, default=3600.0)
    args = parser.parse_args(argv)

    hostname = args.hostname or socket.gethostname()
    limiters = {
        cls: RateLimiter(burst=args.burst, window=args.window) for cls in CLASSES
    }
    for line in journal_lines():
        handle_line(line, args.receiver, hostname, limiters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
