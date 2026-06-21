# GLM-5.2 NVFP4 (REAP, b12x) — vLLM serving throughput on SM120

Online serving benchmark of the GLM-5.2 REAP deployment (see
[../inference/glm-5.2-nvfp4-sm120-design-2026-06.md](../inference/glm-5.2-nvfp4-sm120-design-2026-06.md)),
to compare against the
[MiniMax-M3 baseline](minimax-m3-nvfp4-vllm-serve.md) on the same hardware.

## Setup

- **Model**: `0xSero/GLM-5.2-504B` (REAP-pruned GLM-5.2, ~40B-active MoE, NVFP4)
- **Stack**: b12x image, vLLM `0.11.2.dev279+…cu132.20260608`, TP=4 / DCP=4,
  `max-num-seqs 2`, `max-model-len 250000`, reasoning on, **co-resident with M3**
  (GLM on GPUs 4–7, M3 on 0–3)
- **Hardware**: 4× RTX PRO 6000 Blackwell (SM120, 96 GB), PCIe — no NVLink
- **Tool**: `vllm bench serve`, run inside the container against `127.0.0.1:9212`
- **Workload**: `--dataset-name random`, 1024 in / 512 out, `--ignore-eos`,
  reasoning on. Same workload as the M3 baseline.
- **Date**: 2026-06-21

## Results

| Concurrency | Output tok/s | Total tok/s | Per-stream tok/s (1000/TPOT) | Median TTFT | Mean TPOT |
|---|---|---|---|---|---|
| 1 | 29 † | 87 † | **~44** | 537 ms | 22.7 ms |
| 2 | 59 | 178 | ~31 | 656 ms | 32.7 ms |

† As with M3, the conc=1 *aggregate* is dragged down by a cold-start outlier (first
request after idle: mean TTFT 6.3 s, **P99 TTFT 43 s** — prefill-graph compile).
Steady-state single-stream decode is **~44 tok/s** (TPOT; peak-output confirms 45).

GLM is capped at `max-num-seqs 2`, so concurrency 2 is its ceiling (~59 output
tok/s aggregate); the M3 config goes to 16.

## Comparison to MiniMax-M3

| | GLM-5.2-504B | MiniMax-M3 |
|---|---|---|
| Single-stream decode | **~44 tok/s** (TPOT 22.7 ms) | ~75 tok/s (13.4 ms) |
| Active params/token | ~40B (REAP-pruned) | ~23B |
| `max-num-seqs` | 2 | 16 |
| Saturated output tok/s | ~59 (conc 2) | ~437 (conc 16) |
| Cold-start P99 TTFT | ~43 s | ~31 s |

GLM is **~1.7× slower per token** single-stream — consistent with its larger
active-parameter count (more weight bytes streamed per token; decode is
bandwidth-bound). ~44 tok/s is comfortably interactive for coding/agentic use.
Both share the cold-start prefill-graph compilation cost — warm a session with a
throwaway request before timing latency-sensitive use.

## Reproduce

```bash
# C in {1, 2} (GLM's max-num-seqs ceiling)
podman exec glm52-b12x /opt/venv/bin/vllm bench serve \
  --backend openai-chat --model 0xSero/GLM-5.2-504B \
  --base-url http://127.0.0.1:9212 --endpoint /v1/chat/completions \
  --dataset-name random --random-input-len 1024 --random-output-len 512 \
  --num-prompts $((C*8)) --max-concurrency "$C" --ignore-eos
```
