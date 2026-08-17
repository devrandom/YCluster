# DS4 infernal: watch for DCP support, then TP2+DCP2 vs TP4

The Infernal Invocation launcher (`serve-ds4-flash.sh`) exposes only
`TP_SIZE` and `DCP_SIZE`; there is no PP axis. DCP is hard-gated for DS4
DSpark: dspark mode exits unless `DCP_SIZE=1` ("DSpark non-causal
attention currently requires DCP_SIZE=1"). So today the only way the
4-GPU 256K infernal container can use all four GPUs is TP4.

Measured on r10 TP4/256K / `MAX_NUM_SEQS=16` (the DS4 4-GPU host,
alongside r16 TP2/128K on 9216) with warm, 100%-cached `inference batch`
runs (2026-08-13/14):

- 4k-prompt/256-decode × 16: 2.47 r/s, TTFT 1.84s, e2e 5.82s,
  633.8 tok/s aggregate (r16: 3.02 r/s, TTFT 1.61s, 794.5 tok/s).
- 100k-prompt/256-decode × 16: 1.47 r/s, TTFT 4.79s, e2e 10.08s,
  380.8 tok/s aggregate (r16: 2.03 r/s, TTFT 3.19s, 516.7 tok/s).
- Per-GPU decode at 4k/256 is ~158 tok/s/GPU vs ~397 for r16 → the TP4
  shape is comm/latency-bound on those workloads, not compute-bound.
- KV pool is correct for the long-context role:
  `kv_cache_size_tokens` ≈ 1.66M, `kv_cache_max_concurrency` ≈ 6.4 at
  256K, no corruption-watchdog trips.

The interesting second axis for DS4 is DCP (the long-context axis II
already uses for Kimi K3 / GLM), not PP. When an Infernal Invocation
release lifts the DSpark `DCP_SIZE=1` restriction:

- Test **TP2 + DCP2**: TP2-sized all-reduce (fixes the comm-bound
  shallow-prefix regime) while DCP keeps 256K KV/context spread across
  all 4 GPUs.
- Acceptance: same `inference batch` concurrency-16 runs (4k and 100k),
  a >128K single-context smoke (r16 physically cannot run it), and a
  clean #298 corruption-watchdog pass.

Meanwhile, r10 TP4/256K is effectively qualified (live on 9217,
`/v1/models` OK, parked alongside r16). The docs/runbook still describe
it as pending; decide whether to green-light long-context serving traffic
and update the profile once the cutover choice is made.
