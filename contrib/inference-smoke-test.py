#!/usr/bin/env python3
"""Inference smoke-test harness (quality / long-context / vision).

Model-agnostic: works against any OpenAI-compatible chat backend behind the
cluster gateway. Hits it via contrib/_cluster_config.py (endpoint + bearer from
config.yml), so it runs from a laptop. Re-run after any deploy / image bump.

Suites (pick on the command line, default all):
  A  quality eval   - fixed temp=0 prompts, auto-graded; catches quant drift
  B  long-context   - needle-in-haystack across token depths (sparse attention);
                      depths are capped to the model's max_model_len
  C  vision         - is the multimodal path enabled / does it read images
                      (reports cleanly on text-only models)

Model comes from the MODEL env var or the first non-suite positional argument.

Usage:
  MODEL=<id> ./inference-smoke-test.py                # all suites
  ./inference-smoke-test.py <id> A C                  # quality + vision
  ./inference-smoke-test.py <id> B --max 200000       # needle up to ~200k tok
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cluster_config

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
# Raw reasoning tags that must never leak into content, across model families.
LEAK_TAGS = ("<mm:think>", "</mm:think>", "<think>", "</think>",
             "<reasoning>", "</reasoning>")

cfg = _cluster_config.load()
BASE, KEY = cfg.endpoint, cfg.token


def _post(path, body, timeout):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def chat(messages, model, max_tokens=1024, temperature=0.0, thinking=None, timeout=180):
    """Non-streaming chat. Returns dict with content/reasoning/usage/latency/error."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if thinking is not None:
        # Model families spell the thinking toggle differently; send both keys
        # (a chat template ignores the one it doesn't use). minimax-family reads
        # `thinking_mode` (string); glm/qwen-family read `enable_thinking` (bool).
        body["chat_template_kwargs"] = {
            "thinking_mode": "enabled" if thinking else "disabled",
            "enable_thinking": thinking,
        }
    t0 = time.monotonic()
    try:
        data = _post("/v1/chat/completions", body, timeout)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}",
                "latency": time.monotonic() - t0}
    except Exception as e:  # noqa: BLE001 - surface anything (timeouts, conn reset)
        return {"error": f"{type(e).__name__}: {e}", "latency": time.monotonic() - t0}
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {}) or {}
    return {
        "content": (msg.get("content") or ""),
        "reasoning": (msg.get("reasoning") or msg.get("reasoning_content") or ""),
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage", {}) or {},
        "latency": time.monotonic() - t0,
        "error": None,
    }


def count_tokens(text, model):
    try:
        body = _post("/v1/tokenize", {"model": model, "content": text}, 60)
        return body.get("count") or len(body.get("tokens") or [])
    except Exception:
        return None


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


# ---------------------------------------------------------------- suite A
def has_num(text, n):
    # match the number allowing $, commas, decimals; word-boundary-ish
    return re.search(rf"(?<![\d.]){re.escape(str(n))}(?![\d])", text.replace(",", "")) is not None


def has_word(text, w):
    return w.lower() in text.lower()


def check_json(text, expected):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return False
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False
    return all(obj.get(k) == v for k, v in expected.items())


QUALITY = [
    ("arith",   "What is 17 multiplied by 24? Give just the number.", lambda t: has_num(t, 408)),
    ("speed",   "A train travels 60 km in 1.5 hours. Average speed in km/h?", lambda t: has_num(t, 40)),
    ("gsm8k",   "Natalia sold clips to 48 friends in April, then half as many in May. "
                "How many clips did she sell altogether?", lambda t: has_num(t, 72)),
    ("dozen",   "I have 3 apples and buy 2 dozen more. How many apples total?", lambda t: has_num(t, 27)),
    ("batball", "A bat and a ball cost $1.10 total. The bat costs $1.00 more than the ball. "
                "How much does the ball cost, in cents?", lambda t: has_num(t, 5)),
    ("sally",   "Sally has 3 brothers. Each brother has 2 sisters. How many sisters does Sally have?",
                lambda t: has_num(t, 1)),
    ("strawberry", "How many times does the letter r appear in the word strawberry?",
                lambda t: has_num(t, 3)),
    ("syllog",  "If all bloops are razzies and all razzies are lazzies, are all bloops lazzies? "
                "Answer yes or no.", lambda t: has_word(t, "yes")),
    ("codetrace", "What does this Python print?\n"
                  "def f(x): return x*x + 1\nprint(f(5))", lambda t: has_num(t, 26)),
    ("capital", "What is the capital city of Australia?", lambda t: has_word(t, "canberra")),
    ("gold",    "What is the chemical symbol for gold?", lambda t: has_word(t, "au")),
    ("author",  "Who wrote the novel Pride and Prejudice?", lambda t: has_word(t, "austen")),
    ("strict",  "Reply with exactly the single word BANANA and nothing else.",
                lambda t: t.strip().upper().strip(".!") == "BANANA"),
    ("json",    "Return only a JSON object with integer keys a=1 and b=2. No prose.",
                lambda t: check_json(t, {"a": 1, "b": 2})),
]


def suite_A(model):
    print(f"\n{'='*60}\nSUITE A  quality eval (temp=0, thinking=on)\n{'='*60}")
    passed, results, leaks = 0, [], 0
    for name, prompt, check in QUALITY:
        r = chat([{"role": "user", "content": prompt}], model, max_tokens=4096)
        if r["error"]:
            print(f"  {RED}ERR {RESET} {name:11} {r['error']}")
            results.append({"id": name, "ok": False, "error": r["error"]})
            continue
        content = r["content"]
        leak = any(t in content for t in LEAK_TAGS)
        leaks += leak
        ok = bool(check(content)) and not leak
        passed += ok
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        ans = content.strip().replace("\n", " ")
        print(f"  {tag} {name:11} {r['latency']:5.1f}s  {DIM}{ans[:80]}{RESET}")
        results.append({"id": name, "ok": ok, "latency": r["latency"],
                        "answer": ans[:200], "leak": leak})
    print(f"  --> {passed}/{len(QUALITY)} passed; reasoning leaks: {leaks}")
    return {"suite": "A", "passed": passed, "total": len(QUALITY), "leaks": leaks, "results": results}


# ---------------------------------------------------------------- suite B
NEEDLE = "The secret passphrase is HONEYBADGER-7723."
NEEDLE_ANS = "HONEYBADGER-7723"
FILLER = ("The old forest stayed quiet under a pale and even sky. "
          "Nothing moved between the tall grey trees for a long while. ")


def build_haystack(target_tokens, depth_frac):
    # ~1.3 tokens/word for this filler; build by word budget, place needle at depth.
    words_needed = int(target_tokens / 1.3)
    unit = FILLER.split()
    reps = max(1, words_needed // len(unit))
    body = (" ".join(unit) + " ") * reps
    cut = int(len(body) * depth_frac)
    cut = body.find(" ", cut) + 1 or cut  # snap to word boundary
    return body[:cut] + NEEDLE + " " + body[cut:]


def suite_B(model, max_tokens_target):
    mml = model_max_len(model)
    cap = min(max_tokens_target, (mml - 2000) if mml else max_tokens_target)
    print(f"\n{'='*60}\nSUITE B  long-context needle (sparse attention), up to ~{cap} tok"
          f"{f' (model max {mml})' if mml else ''}\n{'='*60}")
    depths = [d for d in [2000, 16000, 64000, 128000, 200000] if d <= cap]
    if not depths:
        print(f"  {DIM}context too small for needle depths; skipping{RESET}")
        return {"suite": "B", "passed": 0, "total": 0, "results": []}
    results, passed = [], 0
    for target in depths:
        # at the largest depth, sweep position; otherwise just middle
        fracs = [0.1, 0.5, 0.9] if target == depths[-1] else [0.5]
        for frac in fracs:
            hay = build_haystack(target, frac)
            msgs = [{"role": "user",
                     "content": hay + "\n\nWhat is the secret passphrase? "
                                       "Answer with only the passphrase."}]
            # thinking OFF: pure retrieval. Budget has headroom so that even a
            # model ignoring the thinking toggle can still emit the short answer
            # after a reasoning preamble (the needle answer is a few tokens).
            r = chat(msgs, model, max_tokens=256, thinking=False, timeout=600)
            ptok = (r.get("usage") or {}).get("prompt_tokens")
            label = f"~{target//1000}k@{int(frac*100):>2}%"
            if r["error"]:
                print(f"  {RED}ERR {RESET} {label}  ptok={ptok}  {r['error']}")
                results.append({"target": target, "frac": frac, "ok": False, "error": r["error"],
                                "prompt_tokens": ptok})
                continue
            ok = NEEDLE_ANS in r["content"]
            passed += ok
            tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
            print(f"  {tag} {label}  ptok={ptok}  {r['latency']:6.1f}s  "
                  f"fin={r.get('finish_reason')}  {DIM}{r['content'].strip()[:50]}{RESET}")
            results.append({"target": target, "frac": frac, "ok": ok, "prompt_tokens": ptok,
                            "latency": r["latency"], "finish_reason": r.get("finish_reason")})
    total = len(results)
    print(f"  --> {passed}/{total} needles retrieved")
    return {"suite": "B", "passed": passed, "total": total, "results": results}


# ---------------------------------------------------------------- suite C
def make_image(draw_fn, size):
    from PIL import Image, ImageDraw  # noqa: PLC0415
    img = Image.new("RGB", size, "white")
    draw_fn(ImageDraw.Draw(img), size)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _font(px):
    from PIL import ImageFont  # noqa: PLC0415
    for p in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    return ImageFont.load_default()


def suite_C(model):
    print(f"\n{'='*60}\nSUITE C  vision smoke (is multimodal enabled?)\n{'='*60}")
    try:
        def draw1(d, sz):
            d.ellipse([60, 60, 200, 200], fill="red")
            d.text((230, 90), "42", fill="black", font=_font(120))
        def draw2(d, sz):
            d.text((20, 70), "APPLE", fill="black", font=_font(90))
            d.text((sz[0] - 380, 70), "BANANA", fill="black", font=_font(90))
        img_basic = make_image(draw1, (520, 280))
        img_wide = make_image(draw2, (1700, 240))
    except Exception as e:  # noqa: BLE001
        print(f"  {RED}SKIP{RESET} could not build images locally: {e}")
        return {"suite": "C", "skipped": str(e)}

    tests = [
        ("basic", img_basic, "What number and what colored shape are in this image?",
         lambda t: has_num(t, 42) and has_word(t, "red")),
        ("wide", img_wide, "Read the two words in this image, left then right.",
         lambda t: has_word(t, "apple") and has_word(t, "banana")),
    ]
    results, passed = [], 0
    for name, data_url, q, check in tests:
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": q},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        r = chat(msgs, model, max_tokens=512)
        if r["error"]:
            note = "vision likely NOT enabled" if "400" in r["error"] else ""
            print(f"  {RED}ERR {RESET} {name:6} {r['error'][:90]}  {note}")
            results.append({"id": name, "ok": False, "error": r["error"]})
            continue
        ok = bool(check(r["content"]))
        passed += ok
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {tag} {name:6} {r['latency']:5.1f}s  {DIM}{r['content'].strip()[:80]}{RESET}")
        results.append({"id": name, "ok": ok, "answer": r["content"].strip()[:200]})
    print(f"  --> {passed}/{len(tests)} vision checks passed")
    return {"suite": "C", "passed": passed, "total": len(tests), "results": results}


def main():
    raw = sys.argv[1:]
    skip = {raw.index("--max") + 1} if "--max" in raw else set()
    positional = [a for k, a in enumerate(raw) if not a.startswith("-") and k not in skip]
    suites = [a.upper() for a in positional if a.upper() in ("A", "B", "C")] or ["A", "B", "C"]
    model = os.environ.get("MODEL") or next(
        (a for a in positional if a.upper() not in ("A", "B", "C")), None)
    if not model:
        print(f"usage: MODEL=<id> {sys.argv[0]} [A B C] [--max N]   "
              "(or pass <model> as an argument)", file=sys.stderr)
        return 2
    max_b = 200000
    if "--max" in sys.argv:
        max_b = int(sys.argv[sys.argv.index("--max") + 1])

    print(f"Endpoint: {BASE}\nModel:    {model}\nSuites:   {' '.join(suites)}")
    out = {"model": model, "suites": {}}
    if "A" in suites:
        out["suites"]["A"] = suite_A(model)
    if "B" in suites:
        out["suites"]["B"] = suite_B(model, max_b)
    if "C" in suites:
        out["suites"]["C"] = suite_C(model)

    if "--json" in sys.argv:
        print("\n" + json.dumps(out, indent=2))
    print("\nSUMMARY")
    for s, r in out["suites"].items():
        if r.get("skipped"):
            print(f"  {s}: skipped ({r['skipped']})")
        else:
            print(f"  {s}: {r.get('passed')}/{r.get('total')}"
                  + (f"  leaks={r['leaks']}" if "leaks" in r else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
