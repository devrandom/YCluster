# DeepSeek-V4.1-Flash on RTX PRO 6000 / SM120 — staging

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

## Hardware requirements

| Resource | Requirement | Notes |
|---|---|---|
| GPUs | 4× 96 GB RTX PRO 6000 (SM120) | TP4/DCP1; 3 GPUs won't fit the checkpoint |
| NVMe | ≥ 510 GB free for checkpoint + cache headroom | Local SSD, not network FS |
| Host RAM | 64 GiB staging buffer (disk Engram) | ~189 GiB if RAM Engram (not recommended here) |

## Files added

- `config/ansible/admin/install-deepseek-v41-flash.yml` — playbook
- `config/ansible/admin/templates/ds41-setup.sh.j2` — image pull + checkpoint stage/verify
- `config/ansible/admin/templates/ds41-start.sh.j2` — container launch

## Deploy checklist

1. In private host_vars, set `ds41_enabled: true` and `ds41_gpus: "<4 CDI indices>"` on the target node.
2. Run the playbook (setup scripts only; does not start the container).
3. On the host, as dev: `~/bin/ds41-setup.sh --download` (~510 GB transfer).
4. `~/bin/ds41-setup.sh` to verify revision + config.
5. `~/bin/ds41-start.sh` (TP4, disk Engram, port 9218).
6. Qualify: `/health`, `/v1/models`, arithmetic smoke, image smoke, ≥100K needle test, tool-call smoke.
7. Soak overnight; check for silent-corruption signatures (the V4.1 SM120 field reports flag FP8 KV scale-layout corruption at ≥100K as the killer).
8. Register with gateway (playbook second play does this on the storage leader) and alert-audit.

## Rollback

The container is standalone (`ds41-flash` on port 9218). Existing
`deepseek-v4-flash` (r16) and `deepseek-v4-flash-infernal` (r10) are
untouched. To remove: `podman stop ds41-flash && podman rm ds41-flash`.
