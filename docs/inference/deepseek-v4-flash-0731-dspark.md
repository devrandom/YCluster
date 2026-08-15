# DeepSeek-V4-Flash-0731 (DSpark) on RTX PRO 6000 / SM120 — design

Status: **SERVING** `deepseek-ai/DeepSeek-V4-Flash-0731` @ rev
`9e165c30e2704aec5d9d593cce3eebd58bbef1cb` on the DS4 host via a `voipmonitor/vllm`
**Gilded Gnosis r16** container — TP2, 128K context, FP8 KV, no CPU
offload, port 9216, gateway id `deepseek-v4-flash`.

## Where we are right now

- Native FP4+FP8 mixed checkpoint (~160 GB), 284B params (13B active). We run
  the upstream 0731 checkpoint directly (not `nvidia/DeepSeek-V4-Flash-NVFP4`).
- On TP2, weights eat ~81 GiB/GPU at `GPU_MEMORY_UTILIZATION=0.975`, leaving a
  ~**139,619-token** GPU KV pool. Live metrics: `kv_cache_max_concurrency≈1.07`
  at 128K, `MAX_NUM_SEQS=16` config cap, idle KV 0%. So today it serves ~1
  long-context request at a time and tops out around ~136K single-req without
  offload.
- Plan in flight for a 4-GPU long-context run. Implemented as a **dedicated
  alongside script** `~/bin/ds4-infernal-start.sh` (template
  `ds4-infernal-start.sh.j2`) that launches the Infernal Invocation r10 image
  as container **`ds4-infernal`** on the per-host infernal GPU set, TP4, 256K,
  no offload — a separate service on port 9217 / served name
  `deepseek-v4-flash-infernal`. It does not touch the r16 `ds4-dspark`
  container. The r10 default is `MAX_NUM_SEQS=16` (96-row verifier-graph
  envelope); its on-GPU KV pool is ~1.68M tokens
  (`kv_cache_max_concurrency≈6.4` at 256K), so long-context concurrency is
  still KV/compute-bound rather than slot-bound.

## The two `voipmonitor/vllm` image lines (the breadcrumb)

Upstream hub: `rtx6kpro/master/models/deepseek-v4-flash.md`. Same 0731 model
revision is used across both lines — an upgrade is an image-tag change, not a
weight re-download/verification change.

### 1. Gilded Gnosis v20 — `gilded-gnosis-v20-…-cu132-…-rNN` (current line)

- **Deployed:** `…gilded-gnosis-v20-vllm1e9c9c3-sieec30ff-fi801d57a-cu132-20260731-r16`
  (digest `sha256:48518e91cf87dd0c0483c76ff86e81dfc0f46de7e364b46f7a82c481ce08188f`).
- Long-context-relevant fixes land at **r29+**: verifier-capacity fix (DSpark
  correctness above C24 / higher concurrency + longer context) and FULL-graph
  context-KV compression. r30/r31/r33 keep those.
- Documented stable line: **r33**
  `…gilded-gnosis-v20-vllmfa13d33-b12x06db0f4-fi1ac6942-cu132-20260809-r33`
  (digest `sha256:fdde59fed7f9fc12f9fd5ef1b3b3ea8d5097bf10ebad54b348497102c3a83f82`).
- r34 (`…20260810-r34`) is on Docker Hub but has no runbook page yet.
- Runbooks: `ds4dspark-v20.md`, `ds4dspark-v20-r29.md`, `ds4dspark-v20-r33.md`.

### 2. Infernal Invocation — `infernal-invocation-…` (new/different source line)

- A **different vLLM source line**, not a r16→r33 point bump — treat as an
  upgrade that needs a qualification pass on our topology before trusting its
  numbers.
- **r2** `…infernal-invocation-vllm344438d-b12x1584743-fi1ac6942-cu132-20260812-r2`
  (digest `sha256:2fc077dc7d790d6d27f76e8f7d32ffdc278ff4eb47c927b94d26a7d17d9313cc`),
  released 2026-08-12. **cu132 — matches our driver/stack.**
- Long-context-relevant: FULL CUDA-graph DSpark context-KV, a qualified TP4
  profile, native CPU + filesystem KV offload (`KV_OFFLOADING_SIZE` +
  `NATIVE_L2_GB/PATH`), and LMCache.
- TP4 claim (when first flagged): `MAX_NUM_SEQS=8 @ 128K` ≈ 1M GPU-KV tokens →
  4×256K fits **on-GPU with no CPU offload**. The live TP4/256K infernal
  (r4) reported ~1.68M KV tokens
  / `kv_cache_max_concurrency≈6.39` at 256K; the launcher default is now
  `MAX_NUM_SEQS=16`, so verify the post-launch pool/cache metric
  (`cache_config_info`) before tuning further.
- Runbook: `ds4dspark-infernal-invocation-r2.md`.

  **r2 vs r3/r4 (and the PRs in between) — 2026-08-12 dig**

  The II vLLM source line (`local-inference-lab/vllm`, `dev/infernal-invocation`)
  advances beyond r2 through a batch of new feature/fix PR heads:

  - **r2 (`vllm344438d`)** = PRs #285–293. **cu132**, the qualified/documented
    release (`ds4dspark-infernal-invocation-r2.md`). This is what we were going
    to deploy alongside r16.
  - **r3 (`…cu133-torch213-…-r3` = `7c0899fb…`)** = r2 + PRs #294,#295,#296 (adds
    #294, #295, #296 to the r2 head list). Moves to **CUDA 13.3 / torch 2.13**.
  - **r4 (`…cu133-torch213-…-r4` = `21f04805…`)** = r3 + PR **#298**.

  **Moving past r4 (2026-08-13):** Docker Hub holds r7/r9/r10/r11. Only
  r7/r9/r10 have DS4 `infernal-invocation` runbooks; r11 is GLM-only and has
  no DS4 page. r7 is the broad TP2/TP4 perf baseline; r9 adds #302
  (reasoning-aware strict tool-call grammar activation) and #303 (DSpark
  draft FP8 config restore); r10 adds #304 (immutable filesystem KV
  publication). The tool-call path (#302) plus filesystem KV (#304) are why
  we target **r10**. Its qualified profile is TP2/native-offload 128K, so
  our TP4/256K/no-offload shape still needs a local qualification pass.

  What the new PRs fix (all relevant to long-context DSpark K5 serving):
  - **#294** — grammar bitmask widths after draft trimming. Under DSpark,
    trimming one draft token could shift later concurrent requests onto another
    request's structured-output grammar rows. Qualified 160/160 on DS4-0731.
  - **#295** — stop XGrammar draft validation at grammar termination.
  - **#296** — duplicate DeepSeek-V4 tool-closer leakage (`</tool_calls>` shown
    as assistant text).
  - **#298** — **require decode state for FULL CUDA-graph dispatch.** A prompt
    chunk with K+1 tokens could alias a speculative-decode and replay a FULL
    decode CUDA graph over prompt state. **Exposed on DS4-V4-Flash-0731 DSpark
    K5** as *silent* corruption (repeated / raw-token / multilingual output, no
    exception). r3 was literally built as the **buggy control** (corrupt ~1 of
    ~155–159 responses); the state-aware build is clean 160/160 and validated
    150k/300k-token prompts with 40 GiB native KV offload.

  **Implication / decision:**
  - r2 (qualified, cu132) is safe on our host but **lacks #294–298**, incl. the
    #298 silent-corruption FIX that is exactly our long-context K5 profile.
  - The fix only exists in the **undocumented cu133 r4** line, whose host
    driver-610 / cu133 compatibility was unverified.
  - Action: instead of blindly deploying r2, first verify **cu133/r4 runs on
    host driver 610** (pull r4, CDI smoke + short DSpark K5 run, watch the
    watchdog corruption signature).

  **Driver-610 probe (DS4 host) — PASSED**
  - Host driver: `610.43.02` (≥ CUDA 13.x baseline).
  - Pulled r4 (`sha256:21f048058375…`, confirmed by digest) alongside the
    live r16 container — no disruption; `ds4-dspark` stayed Up.
  - CDI/CUDA smoke: `torch 2.13.0`, `torch.version.cuda == 13.3`,
    `torch.cuda.is_available() == True`, device enumerated as RTX PRO 6000
    Blackwell, and a real matmul completed. **cu133/torch2.13 loads on driver
    610.**
  - r10 now ships the r4-era fixes plus #302/#303/#304, so the remaining
    gate before cutover is the **DSpark K5 TP4 256K (no-offload)** run to
    watch the #298 corruption watchdog and verify the actual KV pool from
    `cache_config_info` (distinct from the runtime-load probe above).

## Deployment breadcrumb

- Playbook: `config/ansible/admin/install-deepseek-v4-flash.yml` (`ds4_image`,
  `ds4_infernal_image`, `ds4_revision`, then registers the backend with
  local-ai-proxy).
- Scripts: `templates/ds4-setup.sh.j2` (pull/CDI/checkpoint),
  `templates/ds4-start.sh.j2` (r16 serving), `templates/ds4-infernal-start.sh.j2`
  (r10 alongside).
- Host: `host_vars/<ds4-host>.yml` (`ds4_enabled`, `ds4_gpus` (r16),
  `ds4_infernal_gpus`/`ds4_infernal_served_name`/`ds4_infernal_port` (r10),
  `comfyui_gpu`).
- Pending: launch the r10 `ds4-infernal` container **alongside** the running
  r16 TP2 container (don't disrupt the live one), then re-validate TP4 KV pool
  / `kv_cache_max_concurrency` before/while moving the infernal container into
  service.
- Next target: **infernal-invocation r10** (not gilded r33, not II r2) — it is
  the newest qualified II line (r7 perf base + #302/#303 tool-call/FP8 fixes +
  #304 filesystem KV). The cu133-vs-driver-610 probe on the DS4 host (above)
  **passed** for r4; r10 runs the same cu133/torch2.13 stack.
- **Qualification runs r10 ALONGSIDE r16 — no restart/cutover of the live
  `ds4-dspark` service.** r16 keeps serving on port 9216 the whole time. r10 is
  launched by `~/bin/ds4-infernal-start.sh` as a **separate container
  `ds4-infernal`** on port 9217 / served name `deepseek-v4-flash-infernal`,
  bound only to its per-host GPU set, running the DSpark K5 TP4 256K profile
  with no CPU offload. Watch the #298 corruption watchdog + actual TP4 KV pool
  from the startup log, and read `/v1/models` on 9217. Only after r10 is
  qualified on TP4/256K do we green-light serving traffic (which inherently
  frees r16's GPUs). Neither `ds4-infernal` nor the r10 script ever
  stops/removes `ds4-dspark`.
- GPU/CDI layout is deployment-specific, so exact pins are intentionally not
  documented here; they live in the private `host_vars/<ds4-host>.yml`
  (`ds4_gpus` for r16, `ds4_infernal_gpus` for r10). If the CDI spec was
  regenerated with `--device-name-strategy=index`, CDI indices may not match
  nvidia-smi indices, and they renumber on regen/reboot — re-check
  `nvidia-ctk cdi list` before binding.
