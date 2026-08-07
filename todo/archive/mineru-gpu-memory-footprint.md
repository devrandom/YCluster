# Reduce MinerU's GPU memory footprint — DONE

MinerU hardcodes vLLM's `gpu_memory_utilization` via
`set_default_gpu_memory_utilization()` in `mineru/backend/vlm/utils.py`:
0.5 on cards with more than 8 GB VRAM, 0.7 otherwise. vLLM then
pre-allocates a KV cache sized to fill that budget at `max_model_len`.
For a single-document parser the reserved KV cache ran far larger than
the model weights, holding ~48 GB of the 96 GB card idle and blocking a
co-tenant on the same GPU.

## What we did

`mineru-podman.service` (the `vllm-async-engine` path) now passes an
explicit vLLM budget straight to the container entrypoint:

```
--gpu-memory-utilization 0.2 --max-model-len 8192
```

`mineru-api` accepts extra CLI args and forwards them into the vLLM
`AsyncEngineArgs` (`mineru/backend/vlm/vlm_analyze.py` → `AsyncLLM`),
and an explicit `gpu_memory_utilization` wins over the 0.5 default from
`set_default_gpu_memory_utilization()`.

- `--gpu-memory-utilization 0.2` caps the pre-reserved budget to ~20%
  of the 96 GB card.
- `--max-model-len 8192` MUST stay ≤ 8192: this VLM's
  `max_position_embeddings` is 8192, and vLLM rejects a larger value
  unless `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` (which we avoid).

## Measured (nv2, VM on shared GPU 0 with Nemotron co-tenant)

- Idle MinerU footprint dropped from ~48-49 GB to ~20 GB.
- vLLM start log: `Available KV cache memory: 15.35 GiB`,
  `GPU KV cache size: 1,341,584 tokens`.
- A real `/file_parse` still returns HTTP 200 with the expected
  markdown content.
- The 96 GB card went from ~1 GB free headroom to tens of GB free,
  restoring room for the Nemotron co-tenant.

## Also fixed en route

Verifying the redeploy surfaced a second, unrelated blocker: nv2 was
still on the distro podman 4.9.3, which cannot parse the CDI 0.8.0 spec
(`additionalGids`) that nvidia-container-toolkit 1.19.1 writes, so every
`podman run --device nvidia.com/gpu=N` failed with `unresolvable CDI
devices`. The rest of the fleet already ran the self-compiled 5.8.0
build; nv2 had been missed in that rollout. Running
`admin/install-podman.yml --limit nv2` brought nv2 onto podman 5.8.0,
which parses the spec. See `docs/operations/cdi-spec-regen.md`.

## Acceptance

- [x] Idle run reserves ~20 GB rather than the full 0.5 budget.
- [x] Real parse completes with the KV cache sized for the configured
      budget.
- [x] A co-located workload (Nemotron) runs on the same GPU without OOM
      and with large free headroom.

## Refs

- `config/ansible/app/files/mineru/mineru-podman.service` (budget flags)
- `docs/design/mineru-podman.md` (open-questions note now reflects the
  implemented knob)
