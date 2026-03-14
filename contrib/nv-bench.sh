#!/usr/bin/env bash
# =============================================================================
# Kimi K2.5 Throughput Model Validation Suite
# Hardware: Auto-detected NVIDIA GPUs
# =============================================================================
#
# This script benchmarks MoE models using DP+EP to calibrate the
# first-principles throughput model used for Kimi K2.5.
#
# Usage:
#   ./nv-bench.sh [mixtral|mixtral-tp|kimi-k25|kimi-k25-ep|all|analyze] [--engine vllm|sglang] [--dp N] [--tp N] [--eager] [--batch "1 4 8 16"]
#
# Defaults: engine=vllm, DP=<num GPUs detected>, TP=1, EP=DP (auto), batch="1 4 8 16".
# CUDA_VISIBLE_DEVICES is set to match DP×TP GPUs (0..N-1).
#
# Engine support:
#   --engine vllm   (default) Uses vLLM serve + vllm bench client
#   --engine sglang  Uses SGLang launch_server + vllm bench client (OpenAI-compatible)
#
# Prerequisites:
#   pip install vllm            # always needed (bench client)
#   pip install "sglang[all]"   # if using --engine sglang

set -euo pipefail

# ─── GPU Detection ───────────────────────────────────────────────────────────

detect_gpus() {
    if ! command -v nvidia-smi &>/dev/null; then
        echo "ERROR: nvidia-smi not found. Cannot detect GPUs." >&2
        exit 1
    fi

    # Count GPUs
    DETECTED_GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    if [ "$DETECTED_GPU_COUNT" -eq 0 ]; then
        echo "ERROR: No NVIDIA GPUs detected." >&2
        exit 1
    fi

    # Get GPU model name(s) for display
    DETECTED_GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | sort -u | paste -sd'/' -)
    # Get per-GPU memory in MiB
    DETECTED_GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i 0 | tr -d ' ')

    echo "Detected ${DETECTED_GPU_COUNT}× ${DETECTED_GPU_NAME} (${DETECTED_GPU_MEM} MiB each)"
}

detect_gpus

# ─── Parse arguments ─────────────────────────────────────────────────────────

BENCH_DP="$DETECTED_GPU_COUNT"
BENCH_TP=1
BENCH_EAGER=0
BENCH_AGENTIC=0
BENCH_PREFIX=0
BENCH_ENGINE="vllm"
BENCH_CMD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dp) BENCH_DP="$2"; shift 2 ;;
        --tp) BENCH_TP="$2"; shift 2 ;;
        --eager) BENCH_EAGER=1; shift ;;
        --agentic) BENCH_AGENTIC=1; shift ;;
        --prefix-test) BENCH_PREFIX=1; shift ;;
        --batch) BENCH_BATCHES="$2"; shift 2 ;;
        --engine)
            BENCH_ENGINE="$2"
            if [[ "$BENCH_ENGINE" != "vllm" && "$BENCH_ENGINE" != "sglang" ]]; then
                echo "ERROR: --engine must be 'vllm' or 'sglang', got '$BENCH_ENGINE'" >&2
                exit 1
            fi
            shift 2
            ;;
        *)
            if [ -z "$BENCH_CMD" ]; then
                BENCH_CMD="$1"
            else
                echo "Unknown argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done
BENCH_CMD="${BENCH_CMD:-kimi-k25}"

NUM_GPUS=$(( BENCH_DP * BENCH_TP ))

if [ "$NUM_GPUS" -gt "$DETECTED_GPU_COUNT" ]; then
    echo "ERROR: Requested DP×TP=$NUM_GPUS GPUs but only $DETECTED_GPU_COUNT detected." >&2
    exit 1
fi

# Build CUDA_VISIBLE_DEVICES as 0,1,...,N-1
GPU_LIST=$(seq -s, 0 $(( NUM_GPUS - 1 )))
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-$GPU_LIST}"
# CUDA headers for flashinfer JIT compilation (nvrtc.h etc.)
# Check both system and venv site-packages for the nvrtc headers.
for _nvrtc_dir in \
    /usr/local/lib/python3.12/dist-packages/nvidia/cuda_nvrtc/include \
    "${SGLANG_VENV:-/workspace/venv-sglang}/lib/python3.12/site-packages/nvidia/cuda_nvrtc/include" \
    "${VLLM_VENV:-/workspace/venv-vllm}/lib/python3.12/site-packages/nvidia/cuda_nvrtc/include"; do
    if [ -d "$_nvrtc_dir" ]; then
        export CPLUS_INCLUDE_PATH="$_nvrtc_dir:${CPLUS_INCLUDE_PATH:-}"
        break
    fi
done

BENCH_PORT=8192
if [ "$BENCH_ENGINE" = "sglang" ]; then
    BENCH_PORT=30000
fi

# Prepend venv bin dirs to PATH if they exist. This lets vllm and sglang
# coexist with incompatible deps (different torch versions etc.).
# Override paths with VLLM_VENV / SGLANG_VENV env vars if needed.
# The active engine's venv goes first so its python/libs take priority;
# the other venv is added after so its CLI (e.g. vllm bench) is still found.
VLLM_VENV="${VLLM_VENV:-/workspace/venv-vllm}"
SGLANG_VENV="${SGLANG_VENV:-/workspace/venv-sglang}"
if [ "$BENCH_ENGINE" = "sglang" ]; then
    [ -d "$SGLANG_VENV/bin" ] && export PATH="$SGLANG_VENV/bin:$PATH"
    [ -d "$VLLM_VENV/bin" ]   && export PATH="$PATH:$VLLM_VENV/bin"
else
    [ -d "$VLLM_VENV/bin" ]   && export PATH="$VLLM_VENV/bin:$PATH"
    [ -d "$SGLANG_VENV/bin" ] && export PATH="$PATH:$SGLANG_VENV/bin"
fi

echo "Config: engine=$BENCH_ENGINE  DP=$BENCH_DP  TP=$BENCH_TP  GPUs=$NUM_GPUS/${DETECTED_GPU_COUNT} available  CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "  vllm:   $(command -v vllm 2>/dev/null || echo 'not found')"
echo "  sglang: $(command -v sglang 2>/dev/null || echo 'not found')"

# Kill any leftover server processes (but not this script)
pgrep -f 'vllm|sglang' | grep -v $$ | xargs -r kill 2>/dev/null || true
sleep 2

RESULTS_DIR="./validation-results"
mkdir -p "$RESULTS_DIR"

# Concurrency levels to test (override with --batch "1 4 8 16")
BENCH_BATCHES="${BENCH_BATCHES:-1 4 8 16 32 64}"

# Prompt shape
if [ "$BENCH_PREFIX" -eq 1 ]; then
    # Prefix caching test: 10 agent types share a 2000-token prefix,
    # each with a unique 128-token suffix. Simulates agentic workloads
    # where agents share system prompts and tool schemas.
    PREFIX_LEN=2000
    SUFFIX_LEN=128
    PREFIX_OUTPUT_LEN=256
    PREFIX_NUM_PREFIXES=10
    echo "Mode: prefix-caching (prefix=${PREFIX_LEN}, suffix=${SUFFIX_LEN}, output=${PREFIX_OUTPUT_LEN}, ${PREFIX_NUM_PREFIXES} prefixes)"
elif [ "$BENCH_AGENTIC" -eq 1 ]; then
    RANDOM_INPUT_LEN=128
    RANDOM_OUTPUT_LEN=256
    echo "Mode: agentic (input=${RANDOM_INPUT_LEN}, output=${RANDOM_OUTPUT_LEN})"
else
    RANDOM_INPUT_LEN=512
    RANDOM_OUTPUT_LEN=256
    echo "Mode: mixed prefill+decode (input=${RANDOM_INPUT_LEN}, output=${RANDOM_OUTPUT_LEN})"
fi


# ─── Helper ──────────────────────────────────────────────────────────────────

# Engine-aware argument helpers. Flags that differ between vLLM and SGLang
# are translated here so model configs stay DRY.

# Print the engine-appropriate flag for max context length.
engine_ctx_len() {
    local len="$1"
    if [ "$BENCH_ENGINE" = "sglang" ]; then
        echo "--context-length $len"
    else
        echo "--max-model-len $len"
    fi
}

# Print the engine-appropriate flag for eager/no-graph mode (if enabled).
engine_eager() {
    if [ "$BENCH_EAGER" -eq 1 ]; then
        if [ "$BENCH_ENGINE" = "sglang" ]; then
            echo "--disable-cuda-graph"
        else
            echo "--enforce-eager"
        fi
    fi
}

run_bench() {
    local label="$1"
    if [ "$BENCH_PREFIX" -eq 1 ]; then
        label="${label}-prefix"
    elif [ "$BENCH_AGENTIC" -eq 1 ]; then
        label="${label}-agentic"
    fi
    # Prepend engine name to label so results are distinguishable
    label="${BENCH_ENGINE}-${label}"
    local model_arg="$2"
    local tp="$3"
    local dp="${4:-1}"
    local ep="${5:-false}"
    local extra_args="${6:-}"
    local output_file="${RESULTS_DIR}/${label}.json"
    local port="$BENCH_PORT"

    echo ""
    echo "========================================"
    echo "Benchmark: $label  (engine=$BENCH_ENGINE)"
    echo "  TP=$tp  DP=$dp  EP=$ep"
    echo "========================================"

    # Build serve command based on engine
    local serve_cmd=""
    if [ "$BENCH_ENGINE" = "sglang" ]; then
        serve_cmd="sglang serve \
            --model-path $model_arg \
            --tp-size $tp \
            --mem-fraction-static 0.90 \
            --host 127.0.0.1 --port $port \
            $extra_args"

        if [ "$dp" -gt 1 ]; then
            serve_cmd="$serve_cmd --dp-size $dp"
        fi
        if [ "$ep" = "true" ]; then
            # SGLang uses explicit ep-size; with EP, experts are sharded
            # across dp*tp ranks, same as vLLM's --enable-expert-parallel.
            local ep_size=$(( dp * tp ))
            serve_cmd="$serve_cmd --ep-size $ep_size"
        fi
    else
        serve_cmd="vllm serve $model_arg \
            --tensor-parallel-size $tp \
            --gpu-memory-utilization 0.90 \
            --host 127.0.0.1 --port $port \
            $extra_args"

        if [ "$dp" -gt 1 ]; then
            serve_cmd="$serve_cmd --data-parallel-size $dp"
        fi
        if [ "$ep" = "true" ]; then
            serve_cmd="$serve_cmd --enable-expert-parallel"
        fi
    fi

    echo "Starting $BENCH_ENGINE server..."
    echo "  $serve_cmd"
    eval "$serve_cmd" &
    SERVER_PID=$!

    # Wait for server
    echo "Waiting for server to be ready..."
    for i in $(seq 1 1800); do
        if curl -s "http://127.0.0.1:${port}/health" > /dev/null 2>&1; then
            echo "Server ready after ${i}s"
            break
        fi
        if ! kill -0 $SERVER_PID 2>/dev/null; then
            echo "ERROR: Server process died. Check GPU memory."
            wait $SERVER_PID || true
            return 1
        fi
        sleep 1
    done

    if ! curl -s "http://127.0.0.1:${port}/health" > /dev/null 2>&1; then
        echo "ERROR: Server failed to start after 1800s"
        kill $SERVER_PID 2>/dev/null || true
        return 1
    fi

    # Warmup: a few throwaway requests to trigger CUDA graph capture,
    # Triton JIT, and KV cache setup before the timed runs.
    echo ""
    echo "--- Warmup (4 prompts, not counted) ---"
    vllm bench serve \
        --backend openai-chat \
        --base-url "http://127.0.0.1:${port}" \
        --model "$model_arg" \
        --trust-remote-code \
        --dataset-name random \
        --random-input-len "$RANDOM_INPUT_LEN" \
        --random-output-len "$RANDOM_OUTPUT_LEN" \
        --num-prompts 4 \
        --max-concurrency 2 \
        --endpoint /v1/chat/completions \
        2>&1 | tail -1
    echo "Warmup complete."

    for batch in $BENCH_BATCHES; do
        # Scale prompts with concurrency: enough to reach steady state
        # but not excessive at low concurrency (e.g. batch=1)
        local num_prompts=$(( batch * 6 < 12 ? 12 : batch * 6 ))
        echo ""
        echo "--- Concurrency=$batch ($num_prompts prompts) ---"
        # Request rate: Poisson arrivals targeting the concurrency level.
        # rate = concurrency / avg_request_duration. With ~256 output tokens
        # at ~20ms TPOT, avg duration ≈ 5s, so rate ≈ batch/5.
        # Use "inf" for batch=1 (sequential, no point spacing them out).
        local req_rate="inf"
        if [ "$batch" -gt 1 ]; then
            req_rate=$(python3 -c "print(max(1.0, $batch / 5.0))")
        fi

        if [ "$BENCH_PREFIX" -eq 1 ]; then
            vllm bench serve \
                --backend openai-chat \
                --base-url "http://127.0.0.1:${port}" \
                --model "$model_arg" \
                --trust-remote-code \
                --dataset-name prefix-repetition \
                --prefix-repetition-prefix-len "$PREFIX_LEN" \
                --prefix-repetition-suffix-len "$SUFFIX_LEN" \
                --prefix-repetition-output-len "$PREFIX_OUTPUT_LEN" \
                --prefix-repetition-num-prefixes "$PREFIX_NUM_PREFIXES" \
                --num-prompts "$num_prompts" \
                --request-rate "$req_rate" \
                --max-concurrency "$batch" \
                --endpoint /v1/chat/completions \
                --save-result \
                --result-dir "$RESULTS_DIR" \
                --result-filename "${label}-batch${batch}.json" \
                2>&1 | tee "${RESULTS_DIR}/${label}-batch${batch}.log"
        else
            vllm bench serve \
                --backend openai-chat \
                --base-url "http://127.0.0.1:${port}" \
                --model "$model_arg" \
                --trust-remote-code \
                --dataset-name random \
                --random-input-len "$RANDOM_INPUT_LEN" \
                --random-output-len "$RANDOM_OUTPUT_LEN" \
                --num-prompts "$num_prompts" \
                --request-rate "$req_rate" \
                --max-concurrency "$batch" \
                --endpoint /v1/chat/completions \
                --save-result \
                --result-dir "$RESULTS_DIR" \
                --result-filename "${label}-batch${batch}.json" \
                2>&1 | tee "${RESULTS_DIR}/${label}-batch${batch}.log"
        fi
    done

    # Shutdown
    echo "Stopping $BENCH_ENGINE server..."
    kill $SERVER_PID 2>/dev/null || true
    wait $SERVER_PID 2>/dev/null || true
    sleep 5
}

# ─── Model Configurations ───────────────────────────────────────────────────

run_mixtral() {
    local dp="$BENCH_DP"
    local tp="$BENCH_TP"
    local ep=$(( dp * tp ))

    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Mixtral 8x22B Instruct (141B, AWQ INT4)           ║"
    echo "║   MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-AWQ    ║"
    echo "║   DP=$dp, EP=$ep, TP=$tp on ${NUM_GPUS}× ${DETECTED_GPU_NAME}"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "8 experts, 48 attn heads, 8 KV heads (GQA)."
    echo ""

    local MODEL="MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-AWQ"
    local LABEL="mixtral-8x22b-dp${dp}-ep${ep}-tp${tp}"

    local EXTRA_ARGS="$(engine_ctx_len 4096) --quantization awq_marlin $(engine_eager)"

    run_bench "$LABEL" \
        "$MODEL" \
        "$tp" "$dp" true \
        "$EXTRA_ARGS"
}

run_mixtral_tp() {
    # Mixtral intermediate_size=16384, gate+up=32768. TP must divide 32768 evenly.
    # Valid TP: 1, 2, 4, 8. TP=3 won't work.
    local tp="${BENCH_TP:-2}"
    if (( 32768 % tp != 0 )); then
        echo "ERROR: TP=$tp doesn't divide Mixtral's FFN dimension (32768). Use TP=1,2,4,8."
        return 1
    fi

    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Mixtral 8x22B Instruct (141B, AWQ INT4)           ║"
    echo "║   MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-AWQ    ║"
    # Override CUDA_VISIBLE_DEVICES for TP-only (ignore DP setting)
    local tp_gpus=$(seq -s, 0 $(( tp - 1 )))
    export CUDA_VISIBLE_DEVICES="$tp_gpus"

    echo "║   TP=$tp (no EP, no DP) on ${tp}× ${DETECTED_GPU_NAME}"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "8 experts, 48 attn heads, 8 KV heads (GQA). All weights sharded ${tp}-way."
    echo ""

    local MODEL="MaziyarPanahi/Mixtral-8x22B-Instruct-v0.1-AWQ"
    local LABEL="mixtral-8x22b-tp${tp}"

    local EXTRA_ARGS="$(engine_ctx_len 4096) --quantization awq_marlin $(engine_eager)"

    run_bench "$LABEL" \
        "$MODEL" \
        "$tp" 1 false \
        "$EXTRA_ARGS"
}

run_kimi_k25() {
    # Kimi K2.5: 1T total, 32B activated, 384 experts (8 selected + 1 shared),
    # MLA attention (64 heads, hidden 7168), expert hidden 2048, 256K context.
    # Native INT4 quantization embedded in the checkpoint.
    #
    # Default: TP=all GPUs, DP=1 (no DP). The model is ~500 GB in INT4,
    # so TP across all GPUs is needed just to fit. Use --dp/--tp to override
    # for DP+EP testing when you have enough GPUs for multiple sharded copies.
    local tp="$BENCH_TP"
    local dp="$BENCH_DP"

    # If the user didn't explicitly set --tp, default to all GPUs via TP
    # (not DP) since the model is too large to replicate.
    if [ "$tp" -eq 1 ] && [ "$dp" -eq "$DETECTED_GPU_COUNT" ]; then
        tp="$DETECTED_GPU_COUNT"
        dp=1
    fi

    local ep=$(( dp > 1 ? dp * tp : 0 ))
    local total_gpus=$(( dp * tp ))
    local gpu_list=$(seq -s, 0 $(( total_gpus - 1 )))
    export CUDA_VISIBLE_DEVICES="$gpu_list"

    if (( 7168 % tp != 0 )); then
        echo "ERROR: TP=$tp doesn't divide Kimi K2.5's attention hidden dim (7168). Use TP=1,2,4,7,8."
        return 1
    fi

    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Kimi K2.5 (1T total, 32B active, native INT4)     ║"
    echo "║   moonshotai/Kimi-K2.5                               ║"
    if [ "$dp" -gt 1 ]; then
        echo "║   DP=$dp, EP=$ep, TP=$tp on ${total_gpus}× ${DETECTED_GPU_NAME}"
    else
        echo "║   TP=$tp on ${total_gpus}× ${DETECTED_GPU_NAME}"
    fi
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "384 experts (8 selected + 1 shared), MLA (64 heads), hidden 7168."
    echo ""

    local MODEL="moonshotai/Kimi-K2.5"
    local LABEL
    if [ "$dp" -gt 1 ]; then
        LABEL="kimi-k25-dp${dp}-ep${ep}-tp${tp}"
    else
        LABEL="kimi-k25-tp${tp}"
    fi

    local EXTRA_ARGS="$(engine_ctx_len 4096) --trust-remote-code"
    EXTRA_ARGS="$EXTRA_ARGS --tool-call-parser kimi_k2 --reasoning-parser kimi_k2"
    # vLLM needs these explicitly; SGLang enables them by default
    if [ "$BENCH_ENGINE" != "sglang" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --enable-chunked-prefill --enable-prefix-caching"
    fi
    EXTRA_ARGS="$EXTRA_ARGS $(engine_eager)"

    local use_ep="false"
    if [ "$dp" -gt 1 ]; then
        use_ep="true"
    fi

    run_bench "$LABEL" \
        "$MODEL" \
        "$tp" "$dp" "$use_ep" \
        "$EXTRA_ARGS"
}

run_kimi_k25_ep() {
    # Kimi K2.5 with DP + Expert Parallelism.
    # Requires enough GPUs to hold multiple TP-sharded copies.
    # Default: DP=2, TP=NUM_GPUS/2 (half the GPUs per DP rank).
    local dp="$BENCH_DP"
    local tp="$BENCH_TP"

    # If user didn't override either flag, default to DP=2, TP=GPUs/2.
    # EP shards experts across DP ranks, TP shards dense params across GPUs.
    # With DP=2, TP=4 on 8 GPUs: ~67 GB/GPU (experts/2 + dense/4).
    if [ "$tp" -eq 1 ] && [ "$dp" -eq "$DETECTED_GPU_COUNT" ]; then
        dp=2
        tp=$(( DETECTED_GPU_COUNT / 2 ))
    fi

    local ep=$(( dp * tp ))
    local total_gpus=$(( dp * tp ))

    if [ "$total_gpus" -gt "$DETECTED_GPU_COUNT" ]; then
        echo "ERROR: DP=$dp × TP=$tp = $total_gpus GPUs, but only $DETECTED_GPU_COUNT detected." >&2
        return 1
    fi
    if (( 7168 % tp != 0 )); then
        echo "ERROR: TP=$tp doesn't divide Kimi K2.5's attention hidden dim (7168). Use TP=1,2,4,7,8."
        return 1
    fi

    local gpu_list=$(seq -s, 0 $(( total_gpus - 1 )))
    export CUDA_VISIBLE_DEVICES="$gpu_list"

    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Kimi K2.5 (1T total, 32B active, native INT4)     ║"
    echo "║   moonshotai/Kimi-K2.5                               ║"
    echo "║   DP=$dp, EP=$ep, TP=$tp on ${total_gpus}× ${DETECTED_GPU_NAME}"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "384 experts (8 selected + 1 shared), MLA (64 heads), hidden 7168."
    echo ""

    local MODEL="moonshotai/Kimi-K2.5"
    local LABEL="kimi-k25-dp${dp}-ep${ep}-tp${tp}"

    local EXTRA_ARGS="$(engine_ctx_len 4096) --trust-remote-code"
    EXTRA_ARGS="$EXTRA_ARGS --tool-call-parser kimi_k2 --reasoning-parser kimi_k2"
    if [ "$BENCH_ENGINE" != "sglang" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --enable-chunked-prefill --enable-prefix-caching"
    fi
    EXTRA_ARGS="$EXTRA_ARGS $(engine_eager)"

    run_bench "$LABEL" \
        "$MODEL" \
        "$tp" "$dp" true \
        "$EXTRA_ARGS"
}

# ─── Analysis Script ─────────────────────────────────────────────────────────

generate_analysis() {
    cat > "${RESULTS_DIR}/analyze.py" << 'PYEOF'
#!/usr/bin/env python3
"""Display benchmark results from all runs."""
import json
import sys
from pathlib import Path


def parse_results(filepath):
    """Extract key metrics from vllm bench output."""
    try:
        with open(filepath) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                "output_tok_per_s": data.get("output_throughput", data.get("output_tokens_per_second")),
                "mean_tpot_ms": data.get("mean_tpot_ms", data.get("mean_inter_token_latency_ms")),
                "mean_ttft_ms": data.get("mean_ttft_ms"),
            }
    except (json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./validation-results")

    print("\n" + "=" * 100)
    print("BENCHMARK RESULTS")
    print("=" * 100)
    print(f"{'Config':<50} {'Batch':>5} {'Out tok/s':>10} {'TPOT ms':>10} {'TTFT ms':>10}")
    print("-" * 100)

    rows = []
    for result_file in results_dir.glob("*-batch*.json"):
        data = parse_results(result_file)
        if not data:
            continue

        name = result_file.stem
        # Extract batch from filename (e.g., "mixtral-8x22b-dp3-ep3-tp1-batch4")
        parts = name.rsplit("-batch", 1)
        config = parts[0] if parts else name
        batch_str = parts[1] if len(parts) > 1 else "0"
        try:
            batch_num = int(batch_str)
        except ValueError:
            batch_num = 0

        rows.append((config, batch_num, data))

    for config, batch_num, data in sorted(rows, key=lambda r: (r[0], r[1])):
        out_tok = f"{data['output_tok_per_s']:.1f}" if data.get("output_tok_per_s") else "—"
        tpot = f"{data['mean_tpot_ms']:.1f}" if data.get("mean_tpot_ms") else "—"
        ttft = f"{data['mean_ttft_ms']:.1f}" if data.get("mean_ttft_ms") else "—"

        print(f"{config:<50} {batch_num:>5} {out_tok:>10} {tpot:>10} {ttft:>10}")

    print()


if __name__ == "__main__":
    main()
PYEOF
    chmod +x "${RESULTS_DIR}/analyze.py"
}

# ─── Main ────────────────────────────────────────────────────────────────────

generate_analysis

case "$BENCH_CMD" in
    mixtral)
        run_mixtral
        ;;
    mixtral-tp)
        run_mixtral_tp
        ;;
    kimi-k25)
        run_kimi_k25
        ;;
    kimi-k25-ep)
        run_kimi_k25_ep
        ;;
    all)
        run_mixtral
        run_mixtral_tp
        run_kimi_k25
        run_kimi_k25_ep
        ;;
    analyze)
        python3 "${RESULTS_DIR}/analyze.py" "$RESULTS_DIR"
        exit 0
        ;;
    *)
        echo "Usage: $0 [mixtral|mixtral-tp|kimi-k25|kimi-k25-ep|all|analyze] [--engine vllm|sglang] [--dp N] [--tp N] [--eager] [--agentic] [--prefix-test] [--batch \"1 4 8\"]"
        exit 1
        ;;
esac

echo ""
echo "════════════════════════════════════════════"
echo "Benchmarks complete. Analyze results with:"
echo "  $0 analyze"
echo "════════════════════════════════════════════"

