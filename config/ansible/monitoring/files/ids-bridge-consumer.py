#!/usr/bin/env python3
"""Forward VM bridge guard blocks to the ids-forwarder.

The bridge guard (admin/install-vm-bridge-guard.yml) is a traffic filter, not
an IDS component; it logs one kernel line per blocked frame, rate limited on
the host. This consumer is the IDS side of that interface.

It attributes each line to the instance that owns the source address (the
host's own Incus view, refreshed on a timer), so:

* the alert names the guest, which is what triage needs;
* the rate budget is per guest, so one noisy tenant cannot mask another
  guest's first attempt;
* an instance listed in the quiet file is recorded (kernel log, nftables
  counter, and a `quieted:` line in this service's journal) but not alerted.
  A tenant with a broken VPN client or an agent polling a cloud metadata
  address is not something to wake anyone up for, and it should not depend on
  the tenant fixing their networking.

The alert message carries the instance name but never an address, port or
interface. Addresses stay on the host.

Dependency-free and safe to run from a long-lived systemd unit.
"""

import argparse
import csv
import io
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_PREFIX = "vm-bridge-guard "

# Guard class -> (scenario, alert message prefix). The message is what
# Alertmanager stores and notifies.
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


def parse_line(line):
    """Return (class, source address) for a guard line, or (None, None)."""
    idx = line.find(LOG_PREFIX)
    if idx < 0:
        return None, None
    fields = line[idx + len(LOG_PREFIX):].split()
    if not fields:
        return None, None
    src = None
    for field in fields:
        if field.startswith("SRC="):
            src = field[len("SRC="):]
            break
    return fields[0], src


def instances():
    """Map bridge address -> instance name from the host's own Incus view."""
    result = subprocess.run(
        ["incus", "list", "--format", "csv", "-c", "n,4"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        # Keep the CLI's own message: a sandbox that blocks the Incus client's
        # config directory fails here, and "exit status 1" alone says nothing.
        raise RuntimeError(result.stderr.strip() or "incus list failed")
    out = result.stdout
    mapping = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) < 2:
            continue
        for entry in row[1].splitlines():
            address = entry.split(" ", 1)[0].strip()
            if address:
                mapping[address] = row[0]
    return mapping


class InstanceIndex:
    """Instance lookup with a short TTL; guests start and stop during a day."""

    def __init__(self, ttl=60.0, clock=time.monotonic, lister=instances):
        self._ttl = ttl
        self._clock = clock
        self._lister = lister
        self._at = None
        self._map = {}

    def name_for(self, address):
        if not address:
            return ""
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl:
            try:
                self._map = self._lister()
                self._at = now
            except Exception as exc:  # keep the previous view on failure
                print("ids-bridge-consumer: incus list failed: {}".format(exc),
                      file=sys.stderr)
                self._at = now
        return self._map.get(address, "")


def quiet_instances(path):
    try:
        text = Path(path).read_text()
    except OSError:
        return set()
    return {
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


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


class State:
    """Everything one log line needs: instances, quiet list, per-key limits."""

    def __init__(self, quiet_path, index=None, burst=1, window=3600.0,
                 clock=time.monotonic):
        self.quiet_path = quiet_path
        self.index = index or InstanceIndex(clock=clock)
        self._burst = burst
        self._window = window
        self._clock = clock
        self._limiters = {}

    def limiter(self, key):
        if key not in self._limiters:
            self._limiters[key] = RateLimiter(self._burst, self._window, self._clock)
        return self._limiters[key]


def payload(cls, hostname, instance=""):
    """Build the CrowdSec notification-http body for one guard class."""
    scenario, message = CLASSES[cls]
    if instance:
        message = "{}: {}".format(instance, message)
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


def handle_line(line, receiver, hostname, state, post_fn=None):
    """Post one alert for a guard line. Return True when an alert was sent."""
    cls, source = parse_line(line)
    if cls not in CLASSES:
        return False
    instance = state.index.name_for(source)
    # One budget per guest and class, whether or not the guest is quiet, so a
    # busy guest cannot flood the journal with quieted lines or mask a second
    # guest's first attempt.
    allowed = state.limiter((cls, instance or source or "")).allow()
    if instance and instance in quiet_instances(state.quiet_path):
        if allowed:
            print("quieted: {} from {}".format(cls, instance), flush=True)
        return False
    if not allowed:
        return False
    try:
        (post_fn or post)(receiver, payload(cls, hostname, instance))
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
    parser.add_argument("--quiet-file", default="/etc/ids-bridge-consumer/quiet-instances")
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--burst", type=int, default=1)
    parser.add_argument("--window", type=float, default=3600.0)
    args = parser.parse_args(argv)

    hostname = args.hostname or socket.gethostname()
    state = State(args.quiet_file, burst=args.burst, window=args.window)
    for line in journal_lines():
        handle_line(line, args.receiver, hostname, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
