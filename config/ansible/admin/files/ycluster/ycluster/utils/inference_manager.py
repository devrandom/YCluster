"""
Inference gateway helpers — local-ai-proxy health rendering and
service reload.

Model and disabled-backend state are managed by the `local-ai-proxy`
binary's own subcommands (see ycluster/cli/inference.py). This module
only implements the bits that stay in ycluster: rendering /healthz in
a shape we like, and bouncing the systemd unit.
"""

import subprocess
import sys

import requests

LOCAL_AI_PROXY_URL = "http://localhost:4001"


def show_status(proxy_url=LOCAL_AI_PROXY_URL):
    """Print local-ai-proxy backend + model health from its /healthz."""
    try:
        resp = requests.get(f"{proxy_url}/healthz", timeout=5)
    except requests.ConnectionError as e:
        print(f"Cannot reach local-ai-proxy at {proxy_url}: {e}")
        print("Is the local-ai-proxy service running on this node?")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"{proxy_url}/healthz returned HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    data = resp.json()
    status = data.get("status", "?")
    healthy = data.get("healthy", 0)
    down = data.get("down", 0)
    disabled = data.get("disabled", 0)
    backends = data.get("backends", [])
    models = data.get("models", [])

    if not backends:
        print(f"status: {status}")
        print(data.get("message", "no backends configured"))
        return

    use_color = sys.stdout.isatty()

    def colored(s, code):
        return f"\033[{code}m{s}\033[0m" if use_color else s

    status_color = {
        "ok": "32",        # green
        "degraded": "33",  # yellow
        "down": "31",      # red
        "unknown": "90",   # gray
    }.get(status, "0")
    state_color = {
        "healthy":     "32",
        "down":        "31",
        "disabled":    "90",
        "unavailable": "31",
        "unknown":     "90",
    }

    summary = f"{healthy} healthy / {down} down"
    if disabled:
        summary += f" / {disabled} disabled"
    print(f"status: {colored(status, status_color)}  ({summary})")

    # Backends section
    print()
    print("backends:")
    max_url = max(len(b.get("url", "")) for b in backends)
    for b in backends:
        url = b.get("url", "?")
        state = b.get("state", "?")
        line = f"  {url.ljust(max_url)}  {colored(state, state_color.get(state, '0'))}"
        if b.get("err"):
            err = b["err"]
            if len(err) > 70:
                err = err[:67] + "..."
            line += f"  {err}"
        print(line)

    # Models section
    if models:
        print()
        print("models:")
        max_name = max(len(m.get("name", "")) for m in models)
        for m in models:
            name = m.get("name", "?")
            state = m.get("state", "?")
            line = f"  {name.ljust(max_name)}  {colored(state, state_color.get(state, '0'))}"
            if len(m.get("backends", [])) > 1:
                line += f"  ({len(m['backends'])} backends)"
            print(line)


def reload_proxy():
    """Restart the local-ai-proxy service to reload static YAML config.

    Model and disabled-backend state live in etcd and are hot-reloaded
    by the proxy; reload is only needed for changes to the YAML config
    (listen address, trusted proxies, health intervals, etc.).
    """
    print("Restarting local-ai-proxy service...")
    result = subprocess.run(
        ["systemctl", "restart", "local-ai-proxy.service"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to restart local-ai-proxy: {result.stderr.strip()}")
        sys.exit(1)
    print("local-ai-proxy restarted.")
