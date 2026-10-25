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
