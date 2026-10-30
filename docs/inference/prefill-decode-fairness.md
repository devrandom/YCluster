# Prefill/decode fairness on single-backend LLM servers

A long prefill and an active generation compete for the same engine step. If
the scheduler gives the prefill the whole token budget, generation can appear to
stall even though the backend is healthy and KV cache is far from full. A second,
independent failure mode is admission queueing: long requests occupy every
`max_num_seqs` slot, so new interactive requests wait behind them.

Treat these as separate problems. They need different controls and different
measurements.

## Two mechanisms

### 1. Step-time dilution

Chunked prefill lets a long prompt occupy one scheduling step. With a 4096-token
chunk and ~4.7k prefill tok/s, one chunk is ~0.87 s. A decode in that step gets
one token per 0.87 s, i.e. ~1.1 tok/s instead of tens of tok/s. If the prefill
is scheduled first and consumes the whole budget, decodes may get zero tokens for
that step.

The fix is to reserve decode service time or shorten the prefill chunk. It is not
enough to look at prompt-token counters: `prompt_tokens_total` includes cached
prefix tokens. A 97% prompt-token share can be only a few percent of new compute
when the prefix-cache hit rate is high.

### 2. Admission queueing

`max_num_seqs` (vLLM) or `max_running_requests` (SGLang) is a global admission
cap. A small number of long requests can occupy every slot. Waiting requests only
move when a slot frees, so their TTFT grows without bound even when KV usage is
low. This is a queueing problem, not a prefill-compute problem.

The fixes are admission fairness (per-user/per-model concurrency caps, priority) or
more slots within the KV and graph limits. An ACL deny is an emergency stop, not a
scheduler policy.

### 3. Prefill-budget admission starvation

Admission slots are not the only gate. A waiting request also needs token budget
for its own prompt prefill. With chunked prefill, a long prompt can consume the
whole `max_num_batched_tokens` budget on every step. A waiting request whose
prompt needs any uncached prefill then cannot be admitted, even when free
admission slots and KV capacity exist. It starts only when a long prefill finishes
or the scheduler leaves budget for it.

This is distinct from step-time dilution: an already-running decode is scheduled
first and keeps making progress, but a waiting request cannot become running. It is
also distinct from a full slot table: `num_requests_waiting` can grow while
`num_requests_running` stays below `max_num_seqs`.

Measured example: two cold ~98k prefills with a 4096-token budget and
`max_parallel_prefills=1` serialized. Generation requests submitted during the
prefills got their first token ~22 s later, only ~0.5 s before the last prefill
finished. Almost their whole decode window ran after the prefills. Decode tok/s
measured over that window is uncontended decode, not prefill interference.

## What to measure

Use per-request metrics, not aggregate token counters alone:

- inter-token latency (ITL) per request, especially while a prefill is active
- TTFT and queue time per request
- first-token time relative to prefill start and end; client request start is
  submission, not decode start
- whether each decode window is fully contained inside the prefill span
- aggregate decode tok/s and per-request decode tok/s, computed only over the
  contained overlap
- `num_requests_running` and `num_requests_waiting`, split by reason if exposed
- KV-cache usage and `max_total_num_tokens`
- prefix-cache hit rate for the generation prompts, so the measured window is
  decode-only
- speculative acceptance length, if spec decode is enabled

Run two separate experiments: one long prefill with N decodes, then N long prefills
with N short interactive requests. The first isolates step-time dilution; the second
isolates admission queueing.

## vLLM V1 / R36 fork controls

The R36 fork has explicit scheduler controls. Its defaults keep them off:
`max_parallel_prefills=1`, `prefill_schedule_interval=1`,
`prefill_compute_share` unset, and `long_prefill_token_threshold=0`.

| Goal | Control | Notes |
|---|---|---|
| Reserve decode time | `--prefill-compute-share auto` (or a fixed value such as `0.2`) | Time-based share of contended model execution. `auto` starts at 0.4 and ranges 0.2–0.8. Fixed `0.2` gives prefill at most 20% of contended time. |
| Smooth auto response | `--prefill-compute-half-life smooth` (2 s) or `responsive` (0.5 s) | Only valid with `prefill_compute_share=auto`. |
| Decode cadence | `--prefill-schedule-interval N` | Defers prefill on non-cadence steps while decode is runnable. `2` is 50/50; `4` is 75/25 decode/prefill. |
| Shorten long prefill chunks | `--long-prefill-token-threshold N` | Caps only long prefill chunks, leaving the rest of `max_num_batched_tokens` for decode. |
| Fair prefills | `--max-parallel-prefills N --prefill-policy decode-aware` | Round-robin across prefills plus a lane for the prefill nearest decode. |
| Admission slots | `--max-num-seqs N` | Global slot cap. Bound it by KV capacity and by CUDA-graph capture size. |

`prefill_compute_share` is a time-based weighted virtual-runtime controller:
when decode and prefill are both runnable, it selects one service class per step.
`prefill_schedule_interval` is a simpler fixed cadence. They overlap; start with
one, not both.

Increasing `max_num_batched_tokens` alone does not reserve room for a waiting
request: the long prefill simply consumes the larger budget. Increasing
`max_parallel_prefills` interleaves prefills and improves fairness among them,
but it still gives a waiting request no budget. To admit a waiting request during a
long prefill, cap the long prefill chunk with `long_prefill_token_threshold`, or
reserve service time with `prefill_compute_share`.

`scheduler_reserve_full_isl` is a KV-capacity reservation, not a per-step token
budget. It does not prevent prefill-budget starvation.

Do **not** lower `max_num_batched_tokens` alone and expect decode to be
protected. A prefill can consume the whole smaller budget and still leave decode
at zero. Use `long_prefill_token_threshold` to cap only long prefills.

## SGLang controls

SGLang uses a different scheduler. The closest controls are:

| Goal | Control | Notes |
|---|---|---|
| Decode after prefill | `--prefill-decode-interval N` | Runs N decode batches after a prefill batch before scheduling the next prefill. Default `0` disables it. |
| Shorten prefill batch | `--chunked-prefill-size N`, `--max-prefill-tokens N` | Both bound prefill work. Lower values shorten each prefill batch but may increase TTFT. |
| Fair prefills | `--prefill-max-requests N` | Caps requests per prefill batch. Default is no limit. |
| Admission slots | `--max-running-requests N` | Must be paired with `--cuda-graph-max-bs-decode N`. Exceeding the captured graph size falls back to eager, which is much slower. |
| Bound the queue | `--max-queued-requests N` | Global waiting-queue cap; rejects or aborts instead of queueing forever. Not per-user. |
| Priority | `--schedule-policy priority --enable-priority-scheduling` | Requires the gateway to send a request priority. Preemption can cause recompute. |
| KV retraction | `--retraction-policy priority` | Only relevant when KV is actually full; not a substitute for admission fairness. |

`--enable-mixed-chunk` mixes prefill and decode in one batch, but the current
SGLang build rejects it with speculative decoding. For an MTP/NEXTN deployment,
use `--prefill-decode-interval` and a smaller prefill batch instead of mixed
chunk.

SGLang exposes live load at `/v1/loads`: `num_running_reqs`, `num_waiting_reqs`,
`token_usage`, `max_running_requests`, `max_total_num_tokens`, cache hit rate,
and speculative acceptance length. Use it to distinguish a full slot table from a
full KV pool.

## Gateway-level controls

Engine flags require a container recreate/restart. Gateway controls can be
hot-reloaded and should carry the user-facing policy:

- per-user and per-model in-flight request caps, returning 429 or a bounded queue
- request priority (interactive high, batch low) if the engine supports it
- a bounded queue wait and a queue-depth alert
- per-request prompt/completion token logging for attribution
- ACL deny only as an emergency stop; it is blunt, manual, and has no TTL

A per-user cap is the structural fix for one user occupying every admission
slot. It does not fix step-time dilution for decodes that are already running.

## Quick qualification benchmark

`contrib/inference` has a `fairness` subcommand for a one-command B-A-B check.
Run it on a cluster node or the bastion so the client uses the local
`http://inference.xc` path rather than the public frontend:

```bash
inference fairness <model>
inference fairness <model> --prefills 1 --json
# on the serving node, bypass the gateway's network hop:
inference --local fairness <model>
```

The phases are named `baseline`, `with-prefill`, and `baseline-after`.
The contention shape is deliberate: the generation prompts' prefixes are warmed
first, so the measured generation is decode-only rather than a mixed
prefill+decode request. In `with-prefill` the cold prefills are then launched
first, followed by the generation requests, and every generation stops after
`--generation-seconds` (default 10 s) of decode. The prefill is extrapolated
from one cold 32k pilot to last at least `--min-prefill-seconds` (default 12 s),
so the whole decode window sits inside it. The command fails unless:

- the actual prefill span is at least `--min-prefill-seconds` (default 12 s);
- every generation ended before the last prefill completed (positive
  `containment` margin);
- every generation ran for at least `--min-decode-fraction` of the requested
  decode window (default 0.9);
- the generation prompts hit the prefix cache at least 90%, so the phase
  measures decode, not a second round of prompt prefill.

Defaults:

- 2 generation requests and 2 cold prefill requests
- 10 s decode deadline per generation (`--generation-seconds`)
- 4096-token `max_tokens` ceiling (`--max-decode-tokens`)
- 16k-token generation context (`--context-tokens`)
- prefill extrapolated from one cold 32k pilot
- one B-A-B repeat
- per-request ITL p50/p95/max, decode tok/s, prefill TTFT, and containment
- start/stop lines per request and per-second momentary decode tok/s for the
  `with-prefill` generations

`--context-tokens` is the generation prompt length. `--prefill-tokens` accepts
an integer or `auto`; auto measures one cold 32k prefill, extrapolates the token
count needed to reach `--min-prefill-seconds`, and caps it at the model context.

The start/stop lines are client submission and return, not decode start and end.
The first-token time is the decode start. A run that fails containment is a
valid finding: the engine did not admit the generations during the prefill, so its
decode ratio is uncontended and must not be read as a fairness pass.

The command also fails if streaming is buffered, the p50 ITL ratio exceeds 1.5,
the p95 ITL ratio exceeds 2.0, decode drops below 0.7× baseline, or the
generation requests/prefills diverge from each other. Use `--json` for
machine-readable output.

To verify the running engine's scheduler configuration, read its startup log for
the non-default arguments. Config fields absent from that list are at source
defaults. Environment variables with similar names may be inert if the build does
not read them.

## Experiment matrix

Run each change in isolation against the same load, then combine:

1. Baseline: no scheduler changes.
2. vLLM: `prefill_compute_share=auto`; repeat with fixed `0.2` if auto
   oscillates.
3. vLLM: `prefill_schedule_interval=2` and `4`.
4. vLLM: `long_prefill_token_threshold=1024` and `512`.
5. SGLang: `prefill_decode_interval=3` and `4`.
6. SGLang: `chunked_prefill_size=2048` plus `prefill_decode_interval=3`.
7. Admission: raise slots only within KV/graph limits; add a per-user cap at
   the gateway.
8. Priority: only after the simpler controls are understood.

Acceptance: per-request ITL during prefill does not collapse, aggregate decode
does not regress, TTFT stays within the interactive budget, queue depth drains,
KV usage stays within the pool, and prefix-cache/spec acceptance stay at baseline.

## Pitfalls

- Do not judge prefill load from `prompt_tokens_total` alone. Check the
  prefix-cache hit rate and the actual new-token count.
- Do not raise SGLang `max_running_requests` above
  `cuda_graph_max_bs_decode`; that is an eager-fallback cliff.
- Do not raise vLLM `max_num_seqs` beyond the worst-case KV budget. If the
  scheduler reserves full input length before admission, `max_num_seqs ×
  max_model_len` must fit the KV pool.
- Do not use ACL deny as a routine fairness mechanism. It denies all traffic
  for that user/model and has no TTL.
- Do not report a decode ratio from a run whose generations were not fully
  contained in the prefill span. The window is mostly uncontended decode.
- Do not assume a waiting request will be admitted just because slots and KV are
  free. It still needs token budget for its own prompt prefill.
