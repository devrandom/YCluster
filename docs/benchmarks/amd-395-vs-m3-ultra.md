## AMD RYZEN AI MAX+ 395 w/ Radeon 8060S

```
$ build/bin/llama-bench --mmap 0 -fa 1 -m $MODEL
ggml_cuda_init: GGML_CUDA_FORCE_MMQ:    no
ggml_cuda_init: GGML_CUDA_FORCE_CUBLAS: no
ggml_cuda_init: found 1 ROCm devices:
  Device 0: Radeon 8060S Graphics, gfx1151 (0x1151), VMM: no, Wave Size: 32
| model                          |       size |     params | backend    | ngl | fa | mmap |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -: | ---: | --------------: | -------------------: |
| qwen3moe 30B.A3B Q4_K - Medium |  17.34 GiB |    30.53 B | ROCm       |  99 |  1 |    0 |           pp512 |      1208.05 ± 22.62 |
| qwen3moe 30B.A3B Q4_K - Medium |  17.34 GiB |    30.53 B | ROCm       |  99 |  1 |    0 |           tg128 |         70.74 ± 0.14 |

build: 7ac890213 (7548)
```

## Apple Mac Studio M3 Ultra

```
% llama-bench -fa 1 -m qwen3-30b.gguf   
ggml_metal_library_init: using embedded metal library
ggml_metal_library_init: loaded in 0.009 sec
ggml_metal_device_init: GPU name:   Apple M3 Ultra
ggml_metal_device_init: GPU family: MTLGPUFamilyApple9  (1009)
ggml_metal_device_init: GPU family: MTLGPUFamilyCommon3 (3003)
ggml_metal_device_init: GPU family: MTLGPUFamilyMetal3  (5001)
ggml_metal_device_init: simdgroup reduction   = true
ggml_metal_device_init: simdgroup matrix mul. = true
ggml_metal_device_init: has unified memory    = true
ggml_metal_device_init: has bfloat            = true
ggml_metal_device_init: use residency sets    = true
ggml_metal_device_init: use shared buffers    = true
ggml_metal_device_init: recommendedMaxWorkingSetSize  = 498216.21 MB
| model                          |       size |     params | backend    | threads | fa |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | ------: | -: | --------------: | -------------------: |
| qwen3moe 30B.A3B Q4_K - Medium |  17.34 GiB |    30.53 B | Metal,BLAS |      24 |  1 |           pp512 |      2315.08 ± 17.89 |
| qwen3moe 30B.A3B Q4_K - Medium |  17.34 GiB |    30.53 B | Metal,BLAS |      24 |  1 |           tg128 |         94.87 ± 0.19 |

build: 84bf3c67 (6810)
```
