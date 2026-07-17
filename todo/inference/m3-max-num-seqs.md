# MiniMax-M3 max-num-seqs knob (vendor-hardcoded)

The dark-devotion b12x recipe hardcodes `--max-num-seqs 4` and
`--max-num-batched-tokens 2048` as literals in
`/usr/local/bin/serve-minimax-m3-nvfp4.sh` (line 215 of the `20260621`
image rev) — saturating aggregate decode at ~170 tok/s on our 4-GPU TP=4
deployment (this run, 2026-06-24). GLM's sister recipe on the same image
keeps the knob env-driven (`MAX_NUM_SEQS`, default 8), so the M3 cap is a
**packaging choice**, not a b12x MoE-kernel limit — though M3 uses
`B12X_ATTN` (MSA / PR-stage) where GLM uses `B12X_MLA_SPARSE`, so the
*attention* path may legitimately cap lower.

The doc-baseline `max-num-seqs 16` predates the image rev
(`docs/benchmarks/minimax-m3-nvfp4-vllm-serve.md`), so the recipe was
tuned down since. Concurrency 4 is acceptable for the current user base
(single-stream ~89 tok/s decode, aggregate ~170 tok/s, ~440 ms TTFT at
saturation), and upstream vLLM PR #45381 (native M3 support, see the
reproducible-build item above) should land soon — making this moot.

**No action right now.**

## If the cap ever becomes a real bottleneck

Extend `serve-minimax-m3-named.sh` (the wrapper already sed-patches the
served-name lines; mirror that pattern) to bump `max-num-seqs` +
`max-num-batched-tokens` from env vars, then validate with
`contrib/inference-smoke-test.py` — suite A (quality, temp=0 traps:
17×24=408, bat-and-ball, letter-count, sibling logic) + suite B
(long-context needle ~8k+). Corruption tells: empty/`\0` content,
`finish_reason: "length"` on temp=0, leaked `<mm:think>` tags, wrong
arithmetic.

KV-cache headroom (`gpu-memory-utilization 0.98`, `max-model-len
256000`, fp8 KV) is tight — raising max-num-seqs likely needs
`gpu-memory-utilization` lowered or `--max-model-len` dropped.
