# DeepSeek-V4.1-Flash on RTX PRO 6000 / SM120

Status: **SERVING** on the four-GPU spare node (`ds41-flash`, port 9218,
gateway key `deepseek-v4_1-flash`). Context cap 512K (`MAX_MODEL_LEN=524288`,
engine KV pool 4.7M tokens). Needles verified through 200K via the smoke
suite (7/7) and a direct 430K-token prompt at 80% depth. The R36 image
token normalizer is patched (bind-mounted) to accept Responses API
`input_text`/`output_text` part types for Codex compatibility.

## Model

`deepseek-ai/DeepSeek-V4.1-Flash-1017` @ `1bae0e1088993b9b82de08f59de5be5a34e42170`.
The 1017 checkpoint is a 2026-10-17 weights update to the 0909 launch
model. Same architecture (`deepseek_v41`), config, tokenizer, and prompt
format — only weights differ. ~510 GB on disk (48 safetensors shards).

This is **not** a weights-only update of our current DS4
(`DeepSeek-V4-Flash-0731`, model_type `deepseek_v4`). V4.1 is a different
architecture: causal encoder-decoder (20+20 layers, hidden 5120), 384
routed experts, ~183 GiB of Engram n-gram hash tables, and a ViT vision
tower. It requires the B12X kernel fork, not the r16/r10 DSpark images.

## Runtime image

`localinferencelab/vllm:jovian-judgement-community-20260912-r36`
(digest `sha256:23ab683d7ce32083f33c163df7dfe554b59aba75bbae8f5b8e4b5a2ef590209b`).

R36 is the only B12X release receipt-qualified with **both RAM and SSD
Engram**. R37/R38 exist but only qualify RAM Engram, which needs ~189 GiB
of pinned host RAM — more than our candidate host has available. R36's
SSD mode reads Engram rows from local NVMe via io_uring with a bounded
staging buffer.

The 1017 checkpoint shares config with the 0909 launch model the R36
receipt tested, but has not been independently re-run. Qualification is
ours to execute.

R37, R38, and the newer Karmic Kraken profile channel exist; see
[Karmic Kraken upgrade notes](#karmic-kraken-upgrade-notes). R36 stays
deployed until a newer build is qualified with disk Engram on this host.

## Hardware requirements

| Resource | Requirement | Notes |
|---|---|---|
| GPUs | 4× 96 GB RTX PRO 6000 (SM120) | TP4/DCP1; 3 GPUs won't fit the checkpoint |
| NVMe | ≥ 510 GB free for checkpoint + cache headroom | Local SSD, not network FS |
| Host RAM | 64 GiB staging buffer (disk Engram) | ~189 GiB if RAM Engram (not recommended here) |

## Host PCIe P2P path

The current host is in the public "before" state: IOMMU on, no NVreg P2P
registry keys, ReBAR off, and ACS enabled on the GPU root ports. C1 codegen
decode measures 208–250 tok/s. A public Max-Q report moved 237 → 444 tok/s after
`amd_iommu=off` + NVreg P2P keys + reboot, but that costs VFIO GPU
passthrough. Do not apply it to this host. Prefer 0xSero's DS4.1 recipe; if
IOMMU off is required, target the all-passthrough four-GPU host instead. See
[gpu-p2p-iommu.md](gpu-p2p-iommu.md).

## Measured prompt-depth throughput

Single-stream sweep through the gateway on 2026-10-28, using
`inference bench deepseek-v4_1-flash`: 256 decode tokens, warmup enabled,
three repeats, temperature 0, `ignore_eos`, and a unique prefix so every
prompt is a cold prefill (`cached=0`). This is a prompt-length curve, not the
C1 codegen benchmark in [gpu-p2p-iommu.md](gpu-p2p-iommu.md), and it does not
measure concurrent throughput.

| Prompt tokens | TTFT | Prefill tok/s, median [min-max] | Decode tok/s, median [min-max] |
|---:|---:|---:|---:|
| 865 | 0.57 s | 1,517 [1,459-1,545] | 194.8 [93.5-197.0] |
| 3,387 | 0.71 s | 4,753 [4,688-4,808] | 209.8 [195.5-229.2] |
| 13,469 | 1.88 s | 7,180 [6,918-7,278] | 186.0 [112.0-205.9] |
| 53,739 | 5.74 s | 9,363 [9,087-9,467] | 230.8 [206.5-231.1] |

Decode medians stay in the 186-231 tok/s band across prompt depth. Prefill
throughput rises from 1.5k to 9.4k tok/s as the prompt grows, while TTFT rises
from 0.57 s to 5.74 s. The low decode minima on the short and 13k prompts are
individual-repeat outliers; medians are the useful comparison.

## Karmic Kraken upgrade notes (researched 2026-10-25, not applied)

Upstream moved past the per-model R36–R38 entrypoints to a shared profile
runner (`PROFILE=ds41-flash`, `lil-serve`). Latest channel builds as of
this note:

| Image | Digest |
|---|---|
| `ghcr.io/local-inference-lab/vllm:karmic-kraken-beta-20261020` | `sha256:bc48d33ee167fb7675a59889fe0a7fea539bc6700c53cada89284d594a754b54` |
| `ghcr.io/local-inference-lab/vllm:karmic-kraken-20261020` (stable) | `sha256:94887a58d4c003a06cb2d96ee5522eb7cea216838cfdad318076f9a08e2e762c` |

What changes on top of R36:

- **R37**: kernel-dispatch and projection-tile tuning (+17.7% C1 vs a
  public-source control on +6000-VRAM hardware); corrects named reasoning
  budget `high` to 75. RAM-Engram qualification only.
- **R38**: adaptive-verification padded-graph pricing, page geometry
  256/128, corrected speculative-token metrics. Stock-clock matched run
  vs R37: C1 +14.5%, C8 aggregate +36.8%, prefill +0.4%, KV pool
  1.55M → 4.57M. RAM-Engram qualification only.
- **Karmic Kraken beta (2026-09-20/22)**: shared profile runner, wheel-built
  components, CUDA 13.4.1 / PyTorch 2.14. vs saved R38 on Max-Q clocks:
  C1 +8.0%, C8 +7.65%, prefill +2.7%. Component PRs not yet merged
  upstream ([checklist #808](https://github.com/local-inference-lab/vllm/issues/808)).
- **2026-10-20 builds**: DS4.1 server default `top_p` 0.95 → 1.0, matching
  the 1017 model card. The stable build is wheel-built with only native
  smoke and LMCache contract tests qualified; serving performance is
  unqualified.

Migration notes if we move:

- Not a drop-in image bump: `serve-ds41-jovian.sh` is gone. Launch with
  `-e PROFILE=ds41-flash -e HARDWARE_PROFILE=rtx-pro-6000-pcie` and
  re-declare deployment-specific choices: `PORT=9218`,
  `ENGRAM_TABLE_MEMORY=disk`, served name `deepseek-v4_1-flash` (the
  profile default serves `DeepSeek-V4.1-Flash`), plus context/slot
  overrides.
- Profile defaults differ from ours: RAM Engram (needs ~189 GiB pinned),
  32 slots, automatic context. Our host wants disk Engram, and at the
  512K cap the worst-case slot bound is ~9 (see
  [scheduler fairness](#scheduler-fairness)).
- The staged original checkpoint is supported: mount it and pass
  `MODEL=<path>`; the runner detects an HF-layout original directory.
  Dry-check with `--print-config` before recreating the container.
- Optional lossless FP4-CSF repack
  (`local-inference-lab/DeepSeek-V4.1-Flash-1017-lossless-CSF`): +35% KV
  capacity at a measured 1.2–3.5% decode cost for DS4.1. Separate
  download and qualification.
- The Responses API tokenizer patch is upstream now (the KK tokenizer
  accepts `input_text`/`output_text`); drop the bind mount on KK.
- Disk Engram is functionally tested past R36 but has no repeated
  throughput benchmark in R37/R38/KK receipts — their numbers are
  RAM-Engram on different clocks. Any upgrade re-runs the smoke/needle
  suite plus a matched short benchmark against R36 before serving traffic.

## MoET recipe (r13, alongside deployment)

A second recipe for the same checkpoint, from `sybil-solutions/local-ai-registry`
(0xSero / kacper-daftcode `vLLM-Moet`), deployed by
`admin/install-deepseek-v41-moet.yml` as `ds41-moet`:

| Item | Value |
|---|---|
| launch id | `vllm-moet-deepseek-v4.1-flash-1017-512k-tp4` |
| image | `ghcr.io/sybil-solutions/vllm-moet-dsv41@sha256:08f61e68…` (r13) |
| runtime | vLLM nightly df3bd094 + sm_120 steps 1-28, DSpark k=7 |
| needs | 4× 96 GB sm_120 in one host with GPU P2P; 47 GB host RAM; 520 GB local NVMe |

It differs from the Karmic Kraken profile in what it turns on, not in what it
serves: fused decode MoE kernel, FP4 KV record + MXFP4 indexer keys, MXFP8
target `lm_head`, sparse-MLA gather loads, PCIe P2P all-reduce, and an
`engram_disk` layer that reads the Engram tables from the checkpoint on NVMe
instead of pinning ~330 GB of host shared memory. That last point is why it
fits a 125 GiB host at all — and why the model directory must be local NVMe.

Quality is lossy by design (top-1 agreement 0.974 vs a stock-kernel reference,
mean KL 0.0195, needle PASS at 245K and 510K upstream). Every `VLLM_MOET_*`
switch falls back to the stock path when set to 0.

Throughput expectations, from the reference host (125 GiB RAM, PCIe 5.0,
300 W per card, disk Engram — the same shape as our candidate hosts):

- recipe-kit prose sweep: prefill 8k 8,944 / 32k 10,695 / 64k 11,119 tok/s;
  decode C1 137.6, C2 191.7, C3 202.5, C4 263.2, C8 271.7 tok/s aggregate;
  C1 at 32k 73.1 tok/s.
- `fraser_bench` (2,048-token code bench): median 403, undisturbed reps
  557-573 tok/s at 5.4-5.6 accepted tokens per step.

The widely quoted 600+ tok/s figures are a different configuration: an
upstream `fraser_bench` run at 325 W with +5000 MHz memory clock and the
Engram tables in host RAM (189 GiB). Do not quote them for a disk-Engram
host. Decode is power-bound, so record the GPU power limit with any number.

Operationally, the launcher recreates the container on every start, so the
FlashInfer/DeepGEMM/Triton JIT caches must be bind-mounted out (the playbook
mounts `/root/.cache` from the host) — otherwise each restart pays the ~22 min
first-start autotune again. The unit attaches to the existing container the
same way the Karmic Kraken unit does, so a deploy does not bounce inference.

### Measured on our host (first bring-up)

4× RTX PRO 6000 96 GB (Max-Q, 300 W), single socket, IOMMU on, all four cards
Gen5 x16 with peer access `OK` on every pair; 125 GiB host RAM, disk Engram.

| Measurement | Result |
|---|---|
| engine ready after container start | ~7 min (upstream quotes ~22 min for a cold autotune) |
| C1 codegen, 512 tokens, 3 runs | 377 / 406 / 426 tok/s, median **406** |
| conc 4, 512 tokens each | 653 tok/s aggregate, 163-185 per request |
| prefill at 127K prompt | 17,311 tok/s |
| needle at 127K (50% depth) | PASS |
| steady-state VRAM | 91.6 GB per card |
| host RAM in use while serving | 72 GiB used, 52 GiB available |

That is roughly 1.6-1.9× the Karmic Kraken profile's 208-250 tok/s C1 on the
same card model, with the same checkpoint and the same IOMMU-on host. The gap
to the published 557-573 tok/s bench numbers is the untuned upstream
configuration (325 W, +5000 MHz memory, RAM Engram); do not expect it here.

### Published numbers and image revisions

Two different sets of figures are in circulation; ours matches the newer one.

| Source | Prefill | C1 decode | Notes |
|---|---|---|---|
| r13 submission (2026-10-21), same image/flags/300 W as ours | 8.9k / 9.9k / 10.7k / 11.1k at 8k/16k/32k/64k | 137.6 tok/s prose | **measured on a live box**: `LIVE: shared with agent traffic`, swap at 21 GB |
| vLLM-Moet README, served image `-r21` (2026-10-26) | 163K in 8.6 s (~19k tok/s) | prose ~277, code ~796; C8 ~717 prose / ~2,055 code aggregate | measured at **400 W** per card |
| this host, r13 image, 300 W Max-Q cards | 17.8k–18.2k flat to 55k, 17.3k at 127k | 406 tok/s median (code) | matches the r21 prefill figure, not the r13 one |

So the mismatch is not a version gap. The 9–11k figures are r13, measured on
the *same* image, flags and 300 W power limit we run — but with live agent
traffic sharing the box and the host swapping (21 GB), which the submission
states outright. Its decode shows the same live-vs-idle spread: `fraser_bench`
median 403 tok/s live, 557–573 undisturbed, against our 406 idle. Prefill
landed above that because our box is idle.

r21 then publishes ~19k prefill on an idle box at 400 W, i.e. prefill is
roughly unchanged between the two revisions and the r21 gains are decode-side.

Image revisions (same repository, `ghcr.io/sybil-solutions/vllm-moet-dsv41`):

| Tag | Digest | Stack |
|---|---|---|
| `r13-5d25cff` (deployed here) | `sha256:08f61e680ddab3f1f1b50f92020133c382630f9798ab0ff5729f8931a604f82d` | vLLM-Moet `5d25cff`, steps 1-28 |
| `df3bd094-r21` (newest) | `sha256:ef1e28b719025690e24f0d6406b34cbe1a32c50ec7bbba2dc7358a0048c81a4a` | vLLM-Moet `b0dc737`, steps 1-36 |

r21 adds switches the r13 recipe does not set: `VLLM_MOET_P2P_AR_FUSED_MAX_ROWS=8`
(8, not 16), `VLLM_MOET_P2P_AG=0`, `VLLM_MOET_DFLASH_PREP_GROUPS=1`,
`VLLM_MOET_CANDIDATE_DET_TOPK=1`, `VLLM_MOET_SWA_DECODE_BUILD=1`,
`VLLM_MOET_MOE_TOPKSUM_ADD=1`, `VLLM_MOET_SWA_DRAFT_BUILD=1`, plus
`--gpu-memory-utilization 0.92` and a JIT-cache mount at `/root/.deep_gemm`
alongside `/root/.cache`. It also carries deterministic MoE output above 8
rows (`B12X_DYNAMIC_DETERMINISTIC_OUTPUT=1`), which matters for repeatable
agent runs. The registry has not published an r21 launch manifest yet (main is
still the 2026-10-23 r13 recipe), so r21's serve config comes from the image
README.

### Prefill and decode by depth (measured, client on the serving node)

`inference --endpoint http://127.0.0.1:9219 bench` — 2 repeats, 128 decode
tokens, cold prompts:

| Prompt tokens | TTFT | Prefill tok/s | Decode tok/s |
|---:|---:|---:|---:|
| 6.9k | 0.39 s | 17,761 | 335 |
| 13.8k | 0.77 s | 17,868 | 297 |
| 27.5k | 1.51 s | 18,224 | 303 |
| 55.0k | 3.03 s | 18,144 | 307 |

A 127k prompt measured 17.3k tok/s end to end, so the rate holds with depth:
a cold 500k prefill is roughly half a minute, not the several minutes the
advertised curve implies.

That is ~1.6× the recipe manifest's own prefill sweep (8,944 / 9,937 /
10,695 / 11,119 tok/s at 8k / 16k / 32k / 64k). The manifest notes its
reference box was serving other agent traffic during the sweep, and its
client was not on the node. Treat 9-11k as the number to quote for the
recipe, and this host as measuring above it.

Prefill saturates the GPUs, so concurrency buys nothing. Cold 32k prefills,
no shared prefix, two rounds per point:

| Concurrency | requests/s | TTFT | end-to-end |
|---:|---:|---:|---:|
| 1 | 0.64 | 1.51 s | 1.55 s |
| 2 | 0.66 | 2.25 s | 2.93 s |
| 4 | 0.66 | 3.83 s | 4.82 s |

Throughput is flat within noise and TTFT grows linearly: a second or fourth
prefill queues behind the first rather than sharing compute. That also means
a two-prefill fairness run reports a "combined span" that is just the sum of
two serialized prefills -- do not read it as an aggregate rate.

### Fairness on the MoET recipe (measured, client on the serving node)

Run against `127.0.0.1:<port>` on the GPU node, not through the gateway: a
remote client adds the storage-leader hop and the WAN path to every TTFT and
ITL figure. `inference --endpoint http://127.0.0.1:9219 fairness <model>`
does this.

Stimulus: 2 generations (16k context) plus 2 concurrent cold prefills, decode
deadline 10 s, baseline ~59 tok/s and p50 ITL 15.9 ms.

| Step budget | Long-prefill cap | Prefill rate | p50 ITL under prefill | Decode under prefill |
|---:|---:|---:|---:|---:|
| 4096 (stock) | off | ~20.8k tok/s | 231 ms | 4.3 tok/s (0.07×) |
| 8192 | 4096 | ~7k tok/s | 434 ms | 2.3 tok/s (0.04×) |
| 8192 | 1024 | ~9k tok/s | 136 ms | 7.2 tok/s (0.12×) |

This image schedules a long prefill as its own step, so a running decode
advances about one token per prefill step: decode rate tracks the step's
prefill token count, and every halving of the cap buys decode smoothness by
halving prefill throughput. Raising the budget while capping chunks makes it
worse, not better — the cap and the budget are not independent here.

The Karmic Kraken profile gets 0.71× decode at near-baseline prefill because
its fork has `--prefill-compute-share`, a time-based service-class scheduler.
This image has only `--prefill-schedule-interval` (defer prefill to every Nth
step), which reproduces the same token-per-step dilution rather than
reserving a decode lane. Reproducing the Kraken numbers here needs the fork
patch, not a lower cap.

Practical default: ship the recipe stock shape (4096 budget, cap off). Cap
`LONG_PREFILL_TOKENS` only if smoothest decode matters more than prefill
throughput, and pick it from the table above.

### MoET deploy checklist

1. In private host_vars: `ds41_moet_enabled: true` and `ds41_moet_gpus` set to
   four CDI **GPU-UUID** names (`nvidia-ctk cdi list`). Index names track the
   driver minor number and renumber when cards move between vfio and the host.
2. `admin/setup-gpu-podman.yml` must have run since the last GPU change: it
   emits both index and UUID CDI device names.
3. `admin/install-deepseek-v41-moet.yml --limit <node> -e ds41_moet_autostart=false`
   installs the scripts and the unit without starting anything.
4. As dev on the host: `~/bin/ds41-moet-setup.sh --download` (image ~25 GB on
   disk, checkpoint ~510 GB).
5. Re-run the play without the override (or `~/bin/ds41-moet-start.sh` then
   `systemctl restart ds41-moet`). First start does the FlashInfer autotune.
6. Qualify: `/health`, `/v1/models`, smoke chat, needletest at 100K/245K, then
   the C1 and conc-8/32 sweep against the Karmic Kraken baseline before
   promoting the gateway key.

## Files added

- `config/ansible/admin/install-deepseek-v41-flash.yml` — playbook
- `config/ansible/admin/templates/ds41-setup.sh.j2` — image pull + checkpoint stage/verify
- `config/ansible/admin/templates/ds41-start.sh.j2` — manual container launch/update
- `config/ansible/admin/templates/ds41-flash.service.j2` — systemd boot-start unit

## Scheduler fairness

Chunked prefill shares the engine step with decode. If generation slows while
a long prefill is active, see
[prefill-decode-fairness.md](prefill-decode-fairness.md). The R36 fork exposes
`--prefill-compute-share`, `--prefill-schedule-interval`,
`--long-prefill-token-threshold`, and `--max-parallel-prefills`; its defaults
leave them off. Long-lived requests can also fill every `MAX_NUM_SEQS` slot while
KV usage stays low, which is an admission-queue problem, not a prefill-compute
problem. Bound `MAX_NUM_SEQS` by worst-case KV (`KV pool / MAX_MODEL_LEN`) and
prefer a gateway per-user concurrency cap over a manual ACL deny.

With our 512K cap and 4.7M-token KV pool the worst-case bound is ~9 slots.
The R38/KK qualification's 32 slots pairs with a 131,072-token cap
(32 × 128K ≈ 4.2M ≈ pool); at 512K those slots could demand ~16M tokens.
Keep 4 slots at the 512K cap, or drop the cap to 128K if 32-slot
concurrency is wanted.

### Qualified fairness settings

Benchmarked with the `inference fairness` prefill-containment benchmark
(two cold prefills, generations fully inside them):

```
MAX_NUM_BATCHED_TOKENS=8192
--long-prefill-token-threshold 4096
--prefill-compute-share 0.2
```

These are the `ds41-start.sh` defaults. Measured against the unqualified
baseline (`MAX_NUM_BATCHED_TOKENS=4096`, both fairness flags off):

- waiting generation requests are admitted during long prefills (TTFT 13.5 s
  vs 22 s when queued behind two ~98k-token prefills); the unqualified config
  starves them entirely because a long prefill chunk consumes the whole
  4096-token step budget;
- decode holds 0.71× of baseline throughput and p50 inter-token latency is
  unchanged (0.84×) during full prefill overlap;
- prefill rate stays near baseline (~6700 vs ~7450 tok/s);
- the residual cost is decode tail latency: p95 ITL up to 2.4× baseline while
  a 4096-token chunk is in flight (sub-second stalls). Smaller caps (2048/1024)
  shrink the stalls but cut prefill throughput to 4.1k/2.4k tok/s; 4096 was
  chosen because admission stalls dominate user pain.

Both halves matter: without the cap, an 8192-token chunk consumes the whole
step and reintroduces admission starvation; without the compute share, admitted
decodes collapse to ~1.7 tok/s while a prefill is active. The container env
vars `PREFILL_COMPUTE_SHARE`/`PREFILL_SCHEDULE_INTERVAL` in the R36 image are
inert — pass the CLI flags.

## Deploy checklist

1. In private host_vars, set `ds41_enabled: true` and `ds41_gpus: "<4 CDI indices>"` on the target node.
2. Run the playbook. It installs the setup/launch scripts and the systemd unit, and enables the unit.
3. On the host, as dev: `~/bin/ds41-setup.sh --download` (~510 GB transfer).
4. `~/bin/ds41-setup.sh` to verify revision + config.
5. Start it with `sudo systemctl start ds41-flash` (TP4, disk Engram, port 9218). Use `~/bin/ds41-start.sh` only to recreate/update the container definition.
6. Qualify: `/health`, `/v1/models`, arithmetic smoke, image smoke, ≥100K needle test, tool-call smoke.
7. Soak overnight; check for silent-corruption signatures (the V4.1 SM120 field reports flag FP8 KV scale-layout corruption at ≥100K as the killer).
8. Register with gateway (playbook second play does this on the storage leader) and alert-audit.

## Rollback

The service unit manages the standalone container (`ds41-flash` on port 9218). Existing
`deepseek-v4-flash` (r16) and `deepseek-v4-flash-infernal` (r10) are
untouched. To remove: `sudo systemctl disable --now ds41-flash && podman rm ds41-flash`.
