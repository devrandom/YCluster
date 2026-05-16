#!/usr/bin/env python3
"""
Tiny batch=1 streaming benchmark for OpenAI-compatible chat completions.

Usage:
    bench-chat.py <base_url> <model> [--prompt "..."] [--max-tokens N] [--warmup N] [--runs N]

Reports, per run: time-to-first-token (TTFT), total wall time, completion
tokens, and completion tok/s (excluding TTFT). Also prints the mean across
runs. The warmup run (--warmup 1 by default) compiles Metal kernels on
first call and is discarded.

Only the `usage` block from the final SSE chunk is trusted for token
counts; we don't try to tokenize client-side.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request

DEFAULT_PROMPT = (
    "List the prime numbers below 200, one per line. "
    "After the list, briefly explain the Sieve of Eratosthenes in 3-4 sentences."
)


def stream_chat(base_url: str, model: str, prompt: str, max_tokens: int) -> dict:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.perf_counter()
    ttft: float | None = None
    last_chunk: dict | None = None

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            if not payload:
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if ttft is None:
                # First chunk with a delta containing content or reasoning
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content") or delta.get("reasoning_content"):
                        ttft = time.perf_counter() - t0

            if chunk.get("usage"):
                last_chunk = chunk

    total = time.perf_counter() - t0

    usage = (last_chunk or {}).get("usage") or {}
    completion_tokens = usage.get("completion_tokens", 0)

    gen_time = total - (ttft or 0.0)
    tps = completion_tokens / gen_time if gen_time > 0 and completion_tokens else 0.0

    return {
        "ttft_s": ttft or 0.0,
        "total_s": total,
        "completion_tokens": completion_tokens,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "tps": tps,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base_url", help="e.g. http://m1.yc:8080")
    p.add_argument("model")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--max-tokens", type=int, default=300)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--runs", type=int, default=3)
    args = p.parse_args()

    print(f"# {args.base_url}  model={args.model}  max_tokens={args.max_tokens}")

    for i in range(args.warmup):
        print(f"  warmup {i + 1}/{args.warmup} ...", end=" ", flush=True)
        try:
            r = stream_chat(args.base_url, args.model, args.prompt, max_tokens=50)
            print(f"ok ({r['completion_tokens']} toks in {r['total_s']:.1f}s)")
        except Exception as e:
            print(f"FAIL: {e}")
            return 1

    results: list[dict] = []
    for i in range(args.runs):
        print(f"  run {i + 1}/{args.runs} ...", end=" ", flush=True)
        try:
            r = stream_chat(args.base_url, args.model, args.prompt, args.max_tokens)
        except Exception as e:
            print(f"FAIL: {e}")
            return 1
        print(
            f"ttft={r['ttft_s']:.2f}s  total={r['total_s']:.2f}s  "
            f"ntok={r['completion_tokens']}  tps={r['tps']:.2f}"
        )
        results.append(r)

    tps_values = [r["tps"] for r in results if r["tps"] > 0]
    ttft_values = [r["ttft_s"] for r in results if r["ttft_s"] > 0]
    if tps_values:
        print(
            f"  -> tps mean={statistics.mean(tps_values):.2f}  "
            f"median={statistics.median(tps_values):.2f}  "
            f"best={max(tps_values):.2f}  "
            f"ttft mean={statistics.mean(ttft_values):.2f}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
