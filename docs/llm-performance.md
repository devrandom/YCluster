# LLM Performance Notes

## Context Length Throughput Drop

Large models (e.g., Kimi K2 ~1T MoE) running on Apple Silicon via llama.cpp show an expected ~50% throughput drop around 30K tokens of context. This is caused by the KV cache growing linearly with context length, consuming an increasing share of memory bandwidth.

## Optimization: KV Cache Quantization

Quantize the KV cache to reduce its bandwidth footprint:

```
--cache-type-k q8_0 --cache-type-v q8_0
```

This roughly halves the KV cache memory bandwidth usage compared to the default f16. More aggressive quantization (`q4_0` for values) is possible but may affect output quality.

## Expected Drop by Hardware (models that fit in VRAM)

For models that fit entirely in GPU memory, the throughput drop at 30K context is driven by memory bandwidth. Approximate expected degradation:

| Hardware | Bandwidth | ~Drop at 30K ctx |
|---|---|---|
| M3 Ultra | ~800 GB/s | ~50% |
| RTX 6000 Ada (48 GB) | ~960 GB/s | ~30-40% |
| RTX 6000 Pro Blackwell (96 GB) | ~1,792 GB/s | ~15-25% |
| H100 SXM (80 GB HBM3) | ~3,350 GB/s | ~10-15% |
| B200 (192 GB HBM3e) | ~8,000 GB/s | ~5-10% |

