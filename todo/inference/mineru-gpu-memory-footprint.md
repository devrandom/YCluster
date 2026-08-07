# Reduce MinerU's GPU memory footprint

MinerU hardcodes vLLM's `gpu_memory_utilization` via
`set_default_gpu_memory_utilization()` in `mineru/backend/vlm/utils.py`:
0.5 on cards with more than 8 GB VRAM, 0.7 otherwise. vLLM then
pre-allocates a KV cache sized to fill that budget at `max_model_len`
and the engine's configured concurrency. For a single-document parser
the realistic load is a small number of concurrent requests on short
sequences, so the reserved KV cache ends up many times larger than the
model weights — VRAM is held idle and cannot be used by a workload
co-located on the same GPU.

Two things to fix:

1. **Expose / lower the memory budget.** Today there is no runtime knob
   (no env var, no config key, no CLI flag) to override the value; the
   launcher only sets `MINERU_UDS_PATH`, `MINERU_MODEL_SOURCE`, and
   `MINERU_API_MAX_CONCURRENT_REQUESTS`. Options:
   - a thin launcher/wrapper that injects `gpu_memory_utilization` (and
     optionally a saner `max_model_len`) into the vLLM `AsyncLLM` kwargs
     without patching upstream, or
   - a small downstream patch to `set_default_gpu_memory_utilization()`
     that picks the value dynamically instead of a constant.
   Target: reserve only what the actual document-parsing concurrency
   (a few GiB above the ~2 GiB bf16 weights) needs, leaving the rest of
   a large GPU free for other services.

2. **Correct the stale doc.** `docs/design/mineru-podman.md` states the
   value is auto-set to 0.9; the installed MinerU resolves to 0.5 on
   cards with more than 8 GB VRAM. Update the doc to the real behavior
   and note the knob once it exists.

Acceptance: on a large-VRAM card, an idle run reserves only single-digit
GiB, and a real parse completes with the KV cache sized for the
configured concurrency rather than the card's full budget. Verify with
`nvidia-smi` during an idle run and during a live parse, and confirm a
co-located workload can start on the same GPU without OOM.

Refs:
- `config/ansible/app/files/mineru/` (launcher / unit, current env knobs)
- `docs/design/mineru-podman.md` (stale `gpu_memory_utilization` claim)
