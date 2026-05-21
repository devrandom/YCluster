#!/usr/bin/env python3
"""Streaming chat-completion smoke test.

Endpoint + bearer token come from config.yml at the repo root (see
contrib/_cluster_config.py). The model is passed as the first
positional argument, or via the MODEL environment variable.

Prints reasoning tokens in dim gray and answer tokens at normal weight,
flushing after every chunk so you see the stream unfold live.

Handles both reasoning shapes:
  - delta.reasoning         (OpenAI o1, gpt-oss)
  - delta.reasoning_content (vLLM, MiniMax)
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cluster_config


DIM = "\033[2m"
RESET = "\033[0m"


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODEL")
    if not model:
        print(f"usage: {sys.argv[0]} <model>   (or set MODEL env var)", file=sys.stderr)
        return 2

    cfg = _cluster_config.load()
    base = cfg.endpoint
    key = cfg.token
    url = base + "/v1/chat/completions"

    print(f"Endpoint: {url}")
    print(f"Model:    {model}")
    print()

    # Tokenizer round-trip via the proxy's /tokenize and /detokenize
    # passthrough (llama.cpp native; vLLM accepts the same paths).
    sample = "hello world"
    tok_req = urllib.request.Request(
        base + "/v1/tokenize",
        data=json.dumps({"model": model, "content": sample}).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(tok_req) as resp:
            tok_body = json.loads(resp.read())
        tokens_list = tok_body.get("tokens") or []
        print(f"tokenize:   {sample!r} -> {tokens_list}")

        detok_req = urllib.request.Request(
            base + "/v1/detokenize",
            data=json.dumps({"model": model, "tokens": tokens_list}).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(detok_req) as resp:
            detok_body = json.loads(resp.read())
        roundtrip = detok_body.get("content", "")
        ok = "ok" if roundtrip.strip() == sample else f"MISMATCH: {roundtrip!r}"
        print(f"detokenize: {tokens_list} -> {roundtrip!r}  [{ok}]")
    except urllib.error.HTTPError as e:
        print(f"tokenize: HTTP {e.code} — {e.read().decode('utf-8', 'replace').strip()}")
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
