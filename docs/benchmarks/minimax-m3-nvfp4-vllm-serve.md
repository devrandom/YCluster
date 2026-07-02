# MiniMax-M3 NVFP4 (b12x) — vLLM serving throughput on SM120

> **Historical baseline.** As of 2026-07-02 prod moved to stock vLLM 0.24 +
> MXFP4 + EAGLE3 (~117–169 tok/s single-stream vs the ~75 here; see
> [../inference/minimax-m3-nvfp4-sm120.md](../inference/minimax-m3-nvfp4-sm120.md)).
> These are the b12x-stack numbers that stack is compared against; a
> concurrency sweep of the new stack hasn't been run yet.

Online serving benchmark of the MiniMax-M3 NVFP4 deployment (see
[../inference/minimax-m3-nvfp4-sm120.md](../inference/minimax-m3-nvfp4-sm120.md)).
A concurrency sweep characterising interactive latency and saturation
throughput — also the baseline for comparing GLM-5.2 later.

## Setup

- **Model**: `brandonmusic/minimax-m3-nvfp4` (MiniMax-M3, 428B / 23B-active MoE, NVFP4)
- **Stack**: b12x image, vLLM `0.11.2.dev279+…cu132.20260608`, TP=4, `max-num-seqs 16`, `max-model-len 262144`, reasoning on
- **Hardware**: 4× RTX PRO 6000 Blackwell (SM120, 96 GB), PCIe — no NVLink
- **Tool**: `vllm bench serve`, run **inside the container** against `127.0.0.1:9211` (no gateway/auth overhead)
- **Workload**: `--dataset-name random`, 1024 input / 512 output tokens, `--ignore-eos` (fixed output length), reasoning on (server default `thinking_mode=enabled`)
- **Date**: 2026-06-21

## Results

| Concurrency | Output tok/s | Total tok/s | Per-stream tok/s (1000/TPOT) | Median TTFT | Mean TPOT |
|---|---|---|---|---|---|
| 1  | 40 †  | 134 † | ~75 | 428 ms | 13.4 ms |
| 4  | 183   | 612   | ~50 | 1137 ms | 20.0 ms |
| 8  | 269   | 898   | ~37 | 806 ms | 27.3 ms |
| 16 | 437   | 1458  | ~31 | 701 ms | 32.7 ms |

† The conc=1 *aggregate* is dragged down by a cold-start outlier — the first
request after idle paid prefill-graph compilation (mean TTFT 5.9 s, **P99 TTFT
31 s**). Steady-state single-stream decode is **~75 tok/s** (from TPOT;
peak-output confirms 76).

## Takeaways

- **Interactive (1 user)**: ~75 tok/s decode, sub-500 ms TTFT once warm — snappy
  for a 23B-active model.
- **Saturated (16 = `max-num-seqs`)**: ~437 output tok/s aggregate (~1460 incl.
  prompt); each user still ~31 tok/s, TTFT ~0.7–2 s. Throughput scales cleanly
  1→16 (40→183→269→437) with no collapse.
- **Cost of load**: per-stream decode degrades 75→31 tok/s idle→saturated —
  expected for PCIe-no-NVLink TP=4.
- **Cold start**: the first request after idle pays prefill-graph compilation
  (~31 s here). Front latency-sensitive use with a warmup ping.

## Methodology note — why "Output tok/s" ≠ 1000/TPOT at concurrency 1

`Output token throughput` = total output tokens ÷ total wall-clock, which
**includes** every request's TTFT (prefill), during which no output tokens are
produced. `1/TPOT` is the decode-*only* rate. At concurrency 1 requests run
sequentially, so all prefill time is dead air. The single-stream throughput is
`output_len / (TTFT + output_len × TPOT)`; with this run's mean TTFT (5.88 s,
inflated by the 31 s cold start):
`512 / (5.88 + 512 × 0.01339) ≈ 40` — matching the reported aggregate. With the
warm *median* TTFT (0.43 s) it would be ~70 tok/s, near the 75 decode rate. At
higher concurrency one request's prefill overlaps another's decode, hiding the
gap — which is why aggregate output throughput *rises* (to 437 at conc 16) even
as per-stream decode *falls* (to ~31).

## Reproduce

```bash
# C in {1, 4, 8, 16}; num-prompts scaled to C for bounded runtime
podman exec minimax-m3-b12x /opt/venv/bin/vllm bench serve \
  --backend openai-chat --model brandonmusic/minimax-m3-nvfp4 \
  --base-url http://127.0.0.1:9211 --endpoint /v1/chat/completions \
  --dataset-name random --random-input-len 1024 --random-output-len 512 \
  --num-prompts $((C*6)) --max-concurrency "$C" --ignore-eos
```

For a clean single-stream latency number, send one warmup request first to pay
the prefill-graph compilation before timing.
