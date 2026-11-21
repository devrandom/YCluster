# Inkling 2 Mini on an RTX PRO 6000 (SM120)

**Status: deployed 2026-11-21.** Served from a single 96 GB card
co-tenanted with MinerU; see
[`docs/inference/inkling-2-mini-sm120.md`](../../docs/inference/inkling-2-mini-sm120.md)
for the verified runbook and measurements. This file is the pre-deployment
plan, kept for the decision record.

## Model

- Apache-2.0, not gated, revision `ed40e7da625f594fc9c4990f5f66be24873c22d7`
  (2026-11-20).
- 26B total / 5.4B active looped MoE: 24 physical layers, loop core
  (layers 6-17) runs three times, so 48 layer visits per token. 128 routed
  experts, top-8. Text + image in, text out. 1,048,576-token context.
- Mixed-precision checkpoint: NVFP4 experts in layers 0-19, MXFP8 in
  20-23. **~20.2 GB total** (19.5 GB main safetensors + 0.64 GB bundled
  3-layer DSpark draft head), so one 96 GB card has ample weight headroom.
- Latent KV is the real sizing question: every core layer keeps a KV cache
  per pass (48 caches total), but only 4 of the 24 layers are full
  attention. Start at **262,144** tokens, not the model max. The vLLM
  recipe only gets ~1.8 concurrent 1M-token requests out of a 192 GB
  GB200, which makes 1M on a 96 GB card far from certain. Raise the cap
  only after reading the startup KV pool size.

## Runtime: SGLang first, vLLM as fallback

Our fleet is RTX PRO 6000 / sm_120, which is the case the landing pages
handle least explicitly.

- **SGLang** verifies RTX PRO 6000 at TP1/EP1 in its cookbook, and
  sgl-project/sglang#51926 adds the SM120/SM121 path: looped checkpoint +
  MTP draft, native NVFP4/MXFP8 grouped GEMMs, FP32 LM head GEMV. Use
  `lmsysorg/sglang:inkling-2-mini`, pinned by digest. Digest observed
  2026-11-21: `sha256:efac853b46811ccb760545b7b29e7119bdf907537ccf6e863fac3011fe8b98de`
  (re-resolve and pin again at deploy time; the tag is mutable).
- **vLLM** has a day-0 image (`vllm/vllm-openai:inkling-2-mini`) but its
  recipe is verified on GB200/B300/GB300/H100/H200/DGX Spark, not RTX PRO
  6000. Keep it as the fallback if the SGLang image fails to initialize on
  sm_120.

Do not build a patched image unless a specific sm_120 failure is
reproduced. The upstream SGLang PR covers the known gaps; a local patch
would need a concrete reason and the usual vendored build playbook.

## Placement and sizing

- One host-visible card, TP1/EP1. Do **not** displace an existing model;
  pick the host with a genuinely idle card after checking live
  `nvidia-smi` and `podman ps` (host_vars comments go stale).
- Container/quadlet name: `inkling-2-mini`. Port **9220** (9211-9219 are
  already assigned). Served name: `inkling-2-mini`.
- Container memory cap 64g (same class as Nemotron); the weight load is
  ~20 GB plus JIT/graph capture.
- Start at `--context-length 262144`. If the card has KV headroom, raise it
  toward 1,048,576 and record the measured capacity; do not silently change
  it after the gateway registration is live.

## Launch recipe (verified RTX PRO 6000 cells)

Balanced (no speculative decoding):

```
sglang serve --model-path /model \
  --served-model-name inkling-2-mini \
  --tp 1 --ep 1 \
  --attention-backend fa4 \
  --dtype bfloat16 \
  --page-size 128 \
  --context-length 262144 \
  --grammar-backend xgrammar \
  --enable-multimodal \
  --enable-metrics \
  --reasoning-parser inkling \
  --tool-call-parser inkling2 \
  --host 0.0.0.0 --port 9220
```

DSpark (bundled draft head) adds:

```
SGLANG_OPT_INKLING2_VERIFY_REL_BIAS_KERNEL=1
SGLANG_OPT_DSV41_DSPARK_MID_FOLD=1
SGLANG_OPT_DSV41_DSPARK_PREPARE_INPUTS_ONCE=1
SGLANG_OPT_DSV41_DSPARK_SKIP_PREFILLED_INPUTS=1
--speculative-algorithm DSPARK --speculative-num-draft-tokens 8
--max-running-requests 4
```

`--enable-metrics` is not in the upstream command but is required here:
the SGLang Prometheus job scrapes `sglang_*` metrics, and
`InferenceSGLangCachePressure` reads `sglang_token_usage`.

Ship Balanced first, then A/B DSpark. DSpark caps concurrency at 4, which
matches the recipe's 168 tok/s single-user figure; accept the cap only if
production traffic tolerates it.

## Integration work

Mirror `install-nemotron-3-nano-omni.yml` (single card, checkpoint staging,
gateway registration) and `install-glm53-flash.yml` (SGLang quadlet,
`--enable-metrics`):

- `config/ansible/admin/install-inkling-2-mini.yml`
- `config/ansible/admin/templates/inkling-2-mini.container.j2` — quadlet,
  `Network=host`, one `AddDevice=nvidia.com/gpu=<n>`, read-only checkpoint
  mount, `Restart=always`, linger so it starts at boot.
- `config/ansible/admin/templates/inkling-2-mini-setup.sh.j2` — pull the
  pinned image, CDI smoke test, stage and verify the HF revision (size +
  tree hash), no sudo.
- Per-host opt-in in `host_vars`: `inkling_enabled`, `inkling_gpus`
  (explicit CDI index, never default to 0), `inkling_port` (9220),
  `inkling_served_name`, `inkling_context_length`, `inkling_dspark`.
- Gateway: on the storage leader, `ycluster inference add
  http://<host>.xc:9220 inkling-2-mini`. The proxy hot-reloads; no restart.
- Monitoring needs no static target: the admin API discovers backends
  whose `/v1/models` reports `owned_by=sglang`, and the alert rules and
  interactive probe are model-generic.
- Docs after verification: `docs/inference/inkling-2-mini-sm120.md` with
  the measured numbers, plus an entry in `docs/inference/README.md`.

If the image entrypoint is `python3` rather than the newer `sglang` CLI,
run `python3 -m sglang.launch_server` with the same flags.

## Verification gates

1. Startup: checkpoint loads without sm_120 kernel fallbacks; `/v1/models`
   lists `inkling-2-mini` with `owned_by=sglang`.
2. **Real output, not liveness.** Sample GSM8K prompts; the recipe reports
   93.6-94.2% on all 1319 questions. A 200 plus an 8-token temp-0 reply is
   not evidence (see the M3 note).
3. Image input end to end through the gateway.
4. Tool call through the `inkling2` parser with auto tool choice.
5. Reasoning parser + multi-turn chat; check that thinking blocks render.
6. Long context: needle at 128K, then 256K and 1M if the KV pool allows.
7. Throughput direct and through the gateway; compare single-stream to the
   168 tok/s RTX PRO 6000 reference and record aggregate at concurrency 4.
8. Metrics visible (`sglang_token_usage`, request counters), no alert
   flapping, interactive probe produces samples.
9. Co-tenant check on the host: `nvidia-smi` stays inside the card budget
   under an image prefill.
10. Reboot/linger: the quadlet returns without a login session.

## Risks

- Upstream support is in an unmerged PR; the image tag can move. Pin the
  digest and keep the checkpoint revision pinned.
- sm_120 NVFP4 grouped GEMM bugs have bitten us before (MiniMax-M3);
  verify logits/output, not just that the server answers.
- The looped architecture stores 48 KV caches; 1M context may not fit
  alongside weights and CUDA graphs.
- DSpark acceptance depends on prompts and it caps `max-running-requests`
  at 4; benchmark with realistic agent traffic, not random tokens.
- Day-0 model: keep the rollout reversible and watch the gateway's
  retry/error rates after registration.

## Rollback

Stop the user unit, `ycluster inference disable http://<host>.xc:9220`,
then remove the model entry once traffic has drained. The checkpoint and
image can stay staged; no other backend is touched.

## Sequence

Agent time is minutes per step; wall clock is dominated by transfers and
engine startup.

1. **Feasibility spike (~30 min wall clock, ~10 min hands-on).** Free-card
   check, pull the image (~4 GB), stage the 20 GB checkpoint, launch,
   wait for weight load/JIT, then generate text, send an image, and read
   the startup KV numbers. If SGLang fails on sm_120, repeat with the vLLM
   image (~15-30 min, mostly pulls and load).
2. **Playbook + templates + host_vars (~20 min).** Mechanical mirror of
   the Nemotron/GLM-5.3 plays; no new design surface.
3. **Registration + verification (~20 min).** Gateway add, generation
   through the proxy, metrics and alert check.

Total with a clean pass: roughly 60-75 min wall clock. The estimate only
grows if sm_120 kernels need a workaround, which is the one genuinely
unknown step.
