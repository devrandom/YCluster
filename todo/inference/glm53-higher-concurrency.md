# GLM-5.3-Flash: evaluate higher concurrency (raise max-running-requests)

The 0xSero SM120 SGLang recipe runs with `--max-running-requests 8` locked
with `--cuda-graph-max-bs-decode 8`. Decode CUDA graphs are captured only up
to batch 8 (8 req × 6 NEXTN/MTP draft tokens), and `max-running-requests` is
deliberately clamped to that ceiling: if the running batch ever exceeds the
largest captured graph shape, decode falls back to eager and costs ~4×
per-token latency, an absorbing state (SGLang issue #33483).

Measured on the live deployment (2026-08-29, `inference batch`,
2k-prompt/256-decode, 0% prefix-cache, conc 16/32):

- conc 16: 2.14 r/s, TTFT 1.84s, per-req decode ~79 t/s, **553 aggregate tok/s**
- conc 32: 2.06 r/s, TTFT 5.89s, per-req decode ~72 t/s, **530 aggregate tok/s**

Aggregate throughput plateaus at ~530-553 tok/s because the engine only ever
serves 8 requests at a time; the excess just queues (rising TTFT), with 0
errors. To get past ~550 aggregate tok/s, the two knobs must be raised
together:

1. Raise `--max-running-requests` **and** `--cuda-graph-max-bs-decode` to the
   same value (16/32) in both the vendored `Dockerfile` CMD and
   `glm53-sglang-start.sh.j2`.
2. Expect larger graph-capture memory/time. With 6 draft tokens the decode
   shape is ~batch×7, so this is not free on the 96 GB cards and needs
   re-qualification of `--mem-fraction-static`.

## Acceptance

- Same `inference batch` conc 16/32 runs (and a conc 64 run) after the bump:
  aggregate decode t/s meaningfully above ~553, per-req decode not
  collapsing, TTFT not exploding at the new ceiling.
- Cross-check no eager-fallback tell: per-token decode stays near the
  old per-req curve at the new batch size rather than ~4× worse.
- Re-run the 1M-context smoke to confirm KV-pool math still fits at the
  larger graph + batch shapes.

## Fallback

Raising `max_running_requests` above the current graph ceiling **without**
raising `cuda-graph-max-bs-decode` is strictly worse (eager cliff). If the
graph bump is too costly or memory-tight, keep 8/8 and add a second backend
slice to scale aggregate throughput instead.

See `docs/inference/glm-5.3-flash-deploy-2026-08.md` for the sizing and
concurrency baseline.
