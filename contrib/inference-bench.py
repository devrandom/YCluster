#!/usr/bin/env python3
"""Inference perf bench (prefill + decode), gateway-fronted.

Companion to inference-smoke-test.py: same plumbing (model-agnostic, endpoint +
bearer from config.yml via _cluster_config, so it runs from a laptop), but this
one measures *speed*, not correctness. Use it for before/after numbers when
tuning a backend (max_num_batched_tokens, parallelism layout, ...).

It streams each request so the two halves are separated cleanly:
  - prefill tok/s = prompt_tokens / TTFT      (time to first generated token)
  - decode  tok/s = (gen-1) / (t_end - TTFT)  (steady inter-token rate)
Single in-flight request (concurrency 1): this profiles the latency path that
the PCIe/no-NVLink comms overhead dominates, not aggregate throughput.

Model comes from the MODEL env var or the first positional argument (same as
the smoke test). Prompt depths sweep prefill cost; --gen sets decode length.

Usage:
  MODEL=<id> ./inference-bench.py                       # default depth sweep
  ./inference-bench.py <id> --lens 4000,16000,64000     # custom depths
  ./inference-bench.py <id> --gen 128 --json            # longer decode + json
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cluster_config

# Line-buffer so per-depth rows appear live even when redirected to a file
# (each leg can take a minute; block-buffering would hide all progress).
sys.stdout.reconfigure(line_buffering=True)

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

cfg = _cluster_config.load()
BASE, KEY = cfg.endpoint, cfg.token

# Same neutral filler as the smoke test's needle suite (~1.3 tok/word), so a
# given target maps to roughly the same prompt_tokens here and there.
FILLER = ("The old forest stayed quiet under a pale and even sky. "
          "Nothing moved between the tall grey trees for a long while. ")


def build_prompt(target_tokens):
    words_needed = int(target_tokens / 1.3)
    unit = FILLER.split()
    reps = max(1, words_needed // len(unit))
    body = (" ".join(unit) + " ") * reps
    # Lead with a fresh nonce so token 0 differs every request: vLLM prefix-caches
    # from position 0, so an identical prompt across runs would hit cache and report
    # a fake multi-thousand-tok/s "prefill" (really a KV lookup). A unique prefix
    # busts the whole-prompt match and forces a cold prefill -- the path we tune.
    # Decode length is pinned by ignore_eos; content is irrelevant to the timings.
    return (f"[run {secrets.token_hex(8)}] " + body
            + "\n\nBriefly summarize the passage above.")


def model_max_len(model):
    try:
        req = urllib.request.Request(BASE + "/v1/models",
                                     headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for m in json.loads(r.read()).get("data", []):
                if m.get("id") == model:
                    return m.get("max_model_len")
    except Exception:
        return None


def stream_chat(messages, model, gen, timeout):
    """Stream a completion. Returns timings + usage, or {'error': ...}."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": gen,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
        # ignore_eos: force exactly `gen` decode steps regardless of what the model
        # would say, so decode tok/s is measured over a real sample and not over
        # the 1-2 tokens an early EOS would give (vLLM extension; we bench vLLM).
        "ignore_eos": True,
        # thinking OFF: keep the generation a flat token stream, no reasoning
        # preamble. Both spellings (see smoke test).
        "chat_template_kwargs": {"thinking_mode": "disabled", "enable_thinking": False},
    }
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    t_first = None
    usage = {}
    try:
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
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    # First sign of generation (content or reasoning) ends prefill.
                    if t_first is None and (delta.get("content")
                                            or delta.get("reasoning_content")
                                            or delta.get("reasoning")):
                        t_first = time.monotonic()
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}"}
    except Exception as e:  # noqa: BLE001 - surface timeouts/conn resets
        return {"error": f"{type(e).__name__}: {e}"}
    return {"t0": t0, "t_first": t_first, "t_end": time.monotonic(), "usage": usage}


def bench(model, lens, gen, timeout):
    mml = model_max_len(model)
    print(f"Endpoint: {BASE}\nModel:    {model}"
          + (f"  (max_model_len {mml})" if mml else "") + f"\nDecode:   {gen} tokens\n")
    print(f"  {'ptok':>8}  {'TTFT':>7}  {'prefill':>11}  {'decode':>10}")
    rows = []
    for target in lens:
        if mml and target > mml - 2000:
            print(f"  {DIM}{target:>8}  skip (> max_model_len){RESET}")
            continue
        prompt = build_prompt(target)
        r = stream_chat([{"role": "user", "content": prompt}], model, gen, timeout)
        if r.get("error"):
            print(f"  {RED}ERR {RESET} {target:>8}  {r['error']}")
            rows.append({"target": target, "error": r["error"]})
            continue
        usage = r["usage"] or {}
        ptok = usage.get("prompt_tokens")
        ctok = usage.get("completion_tokens") or gen
        ttft = (r["t_first"] - r["t0"]) if r["t_first"] else None
        dec = (r["t_end"] - r["t_first"]) if r["t_first"] else None
        prefill_tps = (ptok / ttft) if (ptok and ttft) else None
        decode_tps = ((ctok - 1) / dec) if (dec and dec > 0 and ctok > 1) else None
        print(f"  {(ptok or target):>8}  {ttft or 0:6.2f}s  "
              f"{(f'{prefill_tps:7.0f} t/s' if prefill_tps else '      n/a'):>11}  "
              f"{(f'{decode_tps:5.1f} t/s' if decode_tps else '   n/a'):>10}")
        rows.append({"target": target, "prompt_tokens": ptok, "completion_tokens": ctok,
                     "ttft_s": ttft, "prefill_tps": prefill_tps, "decode_tps": decode_tps})
    return {"model": model, "max_model_len": mml, "decode_tokens": gen, "rows": rows}


def main():
    raw = sys.argv[1:]
    def opt(name, default):
        return raw[raw.index(name) + 1] if name in raw else default
    skip = set()
    for name in ("--lens", "--gen", "--timeout"):
        if name in raw:
            skip |= {raw.index(name), raw.index(name) + 1}
    positional = [a for k, a in enumerate(raw) if not a.startswith("-") and k not in skip]
    model = os.environ.get("MODEL") or (positional[0] if positional else None)
    if not model:
        print(f"usage: MODEL=<id> {sys.argv[0]} [--lens 4000,16000,64000] "
              "[--gen 64] [--json]", file=sys.stderr)
        return 2
    lens = [int(x) for x in opt("--lens", "1000,4000,16000,64000").split(",")]
    gen = int(opt("--gen", "64"))
    timeout = int(opt("--timeout", "600"))

    out = bench(model, lens, gen, timeout)
    if "--json" in raw:
        print("\n" + json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
