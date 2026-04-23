#!/usr/bin/env python3
"""Streaming chat-completion smoke test.

Reads endpoint/model/key from environment variables (picked up from
./env.sh by walking up from the script's directory):

  LITELLM_URL    endpoint, or DEFAULT_URL as fallback
  LITELLM_MODEL  model name, or DEFAULT_MODEL as fallback
  CLUSTER_KEY    bearer token

All three are deployment-specific and belong in env.sh (which is
gitignored) — not hardcoded here.

Prints reasoning tokens in dim gray and answer tokens at normal weight,
flushing after every chunk so you see the stream unfold live.

Handles both reasoning shapes:
  - delta.reasoning         (OpenAI o1, gpt-oss)
  - delta.reasoning_content (vLLM, MiniMax)
"""
import json
import os
import re
import sys
import time
import urllib.request


DIM = "\033[2m"
RESET = "\033[0m"


def find_env_sh(start: str) -> str | None:
    """Walk up from start looking for env.sh; returns its path or None."""
    d = os.path.abspath(start)
    while True:
        candidate = os.path.join(d, "env.sh")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_env_sh(path: str) -> None:
    """Apply `export KEY=VALUE` lines from path to os.environ. Values
    already in the environment win — lets callers override via CLI."""
    pat = re.compile(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].rstrip()
            m = pat.match(line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if v.startswith('"') and v.endswith('"') or v.startswith("'") and v.endswith("'"):
                v = v[1:-1]
            os.environ.setdefault(k, v)


def main() -> int:
    env_path = find_env_sh(os.path.dirname(os.path.abspath(__file__)))
    if env_path:
        load_env_sh(env_path)

    url_base = os.environ.get("LITELLM_URL") or os.environ.get("DEFAULT_URL")
    model = os.environ.get("LITELLM_MODEL") or os.environ.get("DEFAULT_MODEL")
    key = os.environ.get("CLUSTER_KEY", "")
    if not url_base or not model:
        print("error: set LITELLM_URL + LITELLM_MODEL (or DEFAULT_URL + DEFAULT_MODEL) in env.sh", file=sys.stderr)
        return 2
    url = url_base.rstrip("/") + "/v1/chat/completions"

    print(f"Endpoint: {url}")
    print(f"Model:    {model}")
    print()

    payload = json.dumps({
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": "Say hello in one word"}],
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    started = time.monotonic()
    ttft: float | None = None
    in_reasoning = False
    bytes_in = 0
    tokens = 0  # count of SSE deltas that carried reasoning or content

    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            bytes_in += len(raw)
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = ev.get("choices", [{}])[0].get("delta", {}) or {}

            reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
            content = delta.get("content") or ""

            if reasoning:
                if ttft is None:
                    ttft = time.monotonic() - started
                if not in_reasoning:
                    sys.stdout.write(DIM)
                    in_reasoning = True
                sys.stdout.write(reasoning)
                sys.stdout.flush()
                tokens += 1

            if content:
                if ttft is None:
                    ttft = time.monotonic() - started
                if in_reasoning:
                    sys.stdout.write(RESET)
                    in_reasoning = False
                sys.stdout.write(content)
                sys.stdout.flush()
                tokens += 1

    if in_reasoning:
        sys.stdout.write(RESET)
    total = time.monotonic() - started
    print()
    print()
    if ttft is None:
        print(f"no content received   total: {total * 1000:.0f}ms   bytes: {bytes_in}")
        return 0

    # Token rate is measured AFTER the first token so it reflects
    # steady-state throughput, not the cold-start prompt-processing
    # cost that ttft already captures. We count SSE deltas, which
    # equal one token per chunk for every backend we run today
    # (vLLM, llama-server, mlx-server) — not strictly portable.
    gen_time = total - ttft
    tok_s = (tokens - 1) / gen_time if gen_time > 0 and tokens > 1 else 0.0
    print(
        f"ttft: {ttft * 1000:.0f}ms   "
        f"tok/s: {tok_s:.1f}   "
        f"tokens: {tokens}   "
        f"total: {total * 1000:.0f}ms   "
        f"bytes: {bytes_in}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
