#!/usr/bin/env python3
"""Measure interactive latency of active vLLM/SGLang backends.

The probe sends one warmup followed by ten sequential small chat completions
directly to each backend discovered from Prometheus. Each request uses a
unique short prompt and a fixed short generation so the result is not
dominated by decode length or prefix-cache hits.

Metrics are written to the node_exporter textfile directory. Only the storage
leader probes, so the dashboard sees one sample stream rather than one per
storage replica.
"""

import json
import os
import secrets
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

TEXTFILE_DIR = Path(
    os.environ.get("INFERENCE_PROBE_DIR", "/var/lib/prometheus/node-exporter")
)
OUT = Path(
    os.environ.get("INFERENCE_PROBE_OUT", TEXTFILE_DIR / "inference_interactive.prom")
)

PROM_URL = os.environ.get("INFERENCE_PROBE_PROM_URL", "http://127.0.0.1:9090")
REQUESTS = max(1, int(os.environ.get("INFERENCE_PROBE_REQUESTS", "10")))
PROMPT_TOKENS = max(4, int(os.environ.get("INFERENCE_PROBE_PROMPT_TOKENS", "64")))
COMPLETION_TOKENS = max(
    1, int(os.environ.get("INFERENCE_PROBE_COMPLETION_TOKENS", "32"))
)
REQUEST_TIMEOUT = float(os.environ.get("INFERENCE_PROBE_REQUEST_TIMEOUT", "60"))
BACKEND_BUDGET = float(os.environ.get("INFERENCE_PROBE_BACKEND_BUDGET", "180"))
LEADER_CMD = os.environ.get("INFERENCE_PROBE_LEADER_CMD", "ycluster cluster leader")
SKIP_LEADER_CHECK = os.environ.get("INFERENCE_PROBE_SKIP_LEADER_CHECK", "").lower() in {
    "1",
    "true",
    "yes",
}

# Roughly 1.3 tokens per word for the neutral filler used by contrib/inference.
_FILLER = (
    "The old forest stayed quiet under a pale and even sky. "
    "Nothing moved between the tall grey trees for a long while. "
)

TARGET_QUERY = (
    "count by (backend, model) "
    '({__name__=~"vllm:num_requests_running|sglang_num_requests_total"})'
)

_METRICS = (
    (
        "ycluster_inference_interactive_ttft_seconds",
        "P50 time to first streamed content token for the synthetic interactive probe.",
    ),
    (
        "ycluster_inference_interactive_e2e_seconds",
        "P50 end-to-end duration for the synthetic interactive probe.",
    ),
    (
        "ycluster_inference_interactive_probe_up",
        "1 when the warmup and all configured probe requests succeeded.",
    ),
    (
        "ycluster_inference_interactive_probe_samples",
        "Number of successful measured requests in the latest probe run.",
    ),
    (
        "ycluster_inference_interactive_probe_failures",
        "Number of failed measured requests in the latest probe run.",
    ),
    (
        "ycluster_inference_interactive_probe_prompt_tokens",
        "Median prompt tokens reported by the backend for successful probes.",
    ),
    (
        "ycluster_inference_interactive_probe_completion_tokens",
        "Median completion tokens reported by the backend for successful probes.",
    ),
    (
        "ycluster_inference_interactive_probe_last_success_timestamp_seconds",
        "Unix time of the latest successful probe sample.",
    ),
)

_GLOBAL_METRICS = (
    (
        "ycluster_inference_interactive_probe_discovery_up",
        "1 when active vLLM/SGLang backends were discovered from Prometheus.",
    ),
    (
        "ycluster_inference_interactive_probe_targets",
        "Number of backend/model pairs selected for probing.",
    ),
    (
        "ycluster_inference_interactive_probe_last_run_timestamp_seconds",
        "Unix time of the latest probe collector run.",
    ),
)


def short_hostname() -> str:
    """Return the hostname without its DNS suffix."""
    return socket.gethostname().strip().split(".", 1)[0].lower()


def storage_leader() -> str | None:
    """Return the elected storage leader, or None when it cannot be read."""
    try:
        proc = subprocess.run(
            shlex.split(LEADER_CMD),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"inference probe: leader lookup failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        print(
            f"inference probe: leader lookup exited {proc.returncode}: {detail}",
            file=sys.stderr,
        )
        return None
    leader = proc.stdout.strip().split(".", 1)[0].lower()
    return leader or None


def build_prompt(target_tokens: int, nonce: str | None = None) -> str:
    """Build a short, unique prompt so every probe pays a cold prefill."""
    nonce = nonce or secrets.token_hex(6)
    words_needed = max(4, int(target_tokens / 1.3))
    unit = _FILLER.split()
    repeats = max(1, words_needed // len(unit))
    body = (" ".join(unit) + " ") * repeats
    return (
        f"[interactive probe {nonce}] {body}\n\n"
        "Reply with a short numbered list of three colors."
    )


def _http_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def discover_targets(
    prom_url: str = PROM_URL, timeout: float = 10
) -> list[tuple[str, str]]:
    """Return unique (backend, model) pairs from active vLLM/SGLang series."""
    query = urllib.parse.urlencode({"query": TARGET_QUERY})
    url = prom_url.rstrip("/") + "/api/v1/query?" + query
    payload = _http_json(url, timeout)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    targets = set()
    for result in payload.get("data", {}).get("result", []):
        metric = result.get("metric") or {}
        backend = metric.get("backend", "")
        model = metric.get("model", "")
        if backend.startswith(("http://", "https://")) and model:
            targets.add((backend.rstrip("/"), model))
    return sorted(targets)


def _request_body(model: str, prompt: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": COMPLETION_TOKENS,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
        "chat_template_kwargs": {
            "thinking_mode": "disabled",
            "enable_thinking": False,
        },
    }


def probe_once(
    backend: str,
    model: str,
    prompt: str,
    timeout: float = REQUEST_TIMEOUT,
) -> dict:
    """Send one streaming probe and return client-side timing and usage."""
    req = urllib.request.Request(
        backend.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(_request_body(model, prompt)).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    t_first = None
    usage = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            obj = json.loads(data)
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                text = (
                    delta.get("content")
                    or delta.get("reasoning_content")
                    or delta.get("reasoning")
                )
                if text and t_first is None:
                    t_first = time.monotonic()
    t_end = time.monotonic()
    if t_first is None:
        raise RuntimeError("stream ended without a content token")
    return {
        "ttft": t_first - t0,
        "e2e": t_end - t0,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens"
        ),
    }


def probe_target(
    backend: str,
    model: str,
    requests: int = REQUESTS,
    backend_budget: float = BACKEND_BUDGET,
) -> dict:
    """Warm up once, then run the measured probes sequentially."""
    started = time.monotonic()
    result = {
        "backend": backend,
        "model": model,
        "samples": 0,
        "failures": 0,
        "warmup": False,
        "error": "",
        "ttft_p50": None,
        "e2e_p50": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "last_success": 0.0,
    }
    try:
        probe_once(
            backend,
            model,
            build_prompt(PROMPT_TOKENS),
            timeout=min(REQUEST_TIMEOUT, max(1.0, backend_budget)),
        )
        result["warmup"] = True
    except Exception as exc:
        result["error"] = f"warmup: {type(exc).__name__}: {exc}"
        return result

    samples = []
    while len(samples) + result["failures"] < requests:
        remaining = backend_budget - (time.monotonic() - started)
        if remaining <= 0.5:
            break
        try:
            sample = probe_once(
                backend,
                model,
                build_prompt(PROMPT_TOKENS),
                timeout=min(REQUEST_TIMEOUT, remaining),
            )
        except Exception as exc:
            result["failures"] += 1
            result["error"] = f"sample: {type(exc).__name__}: {exc}"
            break
        samples.append(sample)
        result["last_success"] = time.time()

    result["samples"] = len(samples)
    if samples:
        result["ttft_p50"] = statistics.median(s["ttft"] for s in samples)
        result["e2e_p50"] = statistics.median(s["e2e"] for s in samples)
        prompt_tokens = [s["prompt_tokens"] for s in samples if s["prompt_tokens"]]
        completion_tokens = [
            s["completion_tokens"] for s in samples if s["completion_tokens"]
        ]
        if prompt_tokens:
            result["prompt_tokens"] = statistics.median(prompt_tokens)
        if completion_tokens:
            result["completion_tokens"] = statistics.median(completion_tokens)
    return result


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(result: dict) -> str:
    return (
        f'backend="{_escape_label(result["backend"])}",'
        f'model="{_escape_label(result["model"])}"'
    )


def _metric_line(name: str, labels: str, value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{name}{{{labels}}} {value:.6g}\n"


def render(results: list[dict], discovery_up: bool, targets: int, now: float) -> str:
    """Render the complete textfile body."""
    out = []
    for name, help_text in _GLOBAL_METRICS:
        out.append(f"# HELP {name} {help_text}\n")
        out.append(f"# TYPE {name} gauge\n")
    out.append(
        f"ycluster_inference_interactive_probe_discovery_up {1 if discovery_up else 0}\n"
    )
    out.append(f"ycluster_inference_interactive_probe_targets {targets}\n")
    out.append(
        f"ycluster_inference_interactive_probe_last_run_timestamp_seconds {now:.6g}\n"
    )

    for name, help_text in _METRICS:
        out.append(f"# HELP {name} {help_text}\n")
        out.append(f"# TYPE {name} gauge\n")

    for result in results:
        labels = _labels(result)
        up = (
            result["warmup"]
            and result["samples"] == REQUESTS
            and result["failures"] == 0
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_ttft_seconds",
                labels,
                result["ttft_p50"],
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_e2e_seconds", labels, result["e2e_p50"]
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_probe_up", labels, 1 if up else 0
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_probe_samples",
                labels,
                result["samples"],
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_probe_failures",
                labels,
                result["failures"],
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_probe_prompt_tokens",
                labels,
                result["prompt_tokens"],
            )
        )
        out.append(
            _metric_line(
                "ycluster_inference_interactive_probe_completion_tokens",
                labels,
                result["completion_tokens"],
            )
        )
        if result["last_success"]:
            out.append(
                _metric_line(
                    "ycluster_inference_interactive_probe_last_success_timestamp_seconds",
                    labels,
                    result["last_success"],
                )
            )
    return "".join(out)


def write_output(body: str) -> None:
    """Atomically replace the textfile so node_exporter never sees a partial write."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{OUT.name}.", suffix=".tmp", dir=OUT.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    if not SKIP_LEADER_CHECK:
        leader = storage_leader()
        if leader is None:
            print(
                "inference probe: cannot determine the storage leader; "
                "keeping the previous output",
                file=sys.stderr,
            )
            return 1
        if leader != short_hostname():
            try:
                OUT.unlink()
            except FileNotFoundError:
                pass
            print("inference probe: not the storage leader; skipped", file=sys.stderr)
            return 0

    now = time.time()
    try:
        targets = discover_targets()
    except Exception as exc:
        print(f"inference probe: target discovery failed: {exc}", file=sys.stderr)
        write_output(render([], False, 0, now))
        return 1

    results = [probe_target(backend, model) for backend, model in targets]
    for result in results:
        if result["error"]:
            print(
                f'inference probe: {result["model"]} at {result["backend"]}: '
                f'{result["error"]}',
                file=sys.stderr,
            )
    write_output(render(results, True, len(targets), now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
