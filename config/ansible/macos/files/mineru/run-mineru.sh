#!/usr/bin/env bash
# MinerU (opendatalab/MinerU 3.4.x) launcher for macOS cluster nodes
# (m1+). Run by the com.ycluster.mineru LaunchDaemon (as user `dev`)
# — see config/ansible/macos/setup-mineru.yml. Can also be run by hand
# under `dev` to watch the startup banner and MLX init.
#
# Prerequisites on the mac (all provisioned by setup-mineru.yml):
#   - uv installed (brew install uv)
#   - mineru[all] installed into ~/mineru-venv (uv pip install)
#   - HF cache directory MINERU_HF_CACHE exists and is dev-writable
#   - mineru-models-download has been run once (populates the cache)
#
# Listens on :8000 with no API key — cluster-internal auth is handled
# upstream by local-ai-proxy + nginx (Open-WebUI api_key table).
# Register this node as a local-ai-proxy backend with:
#   ycluster inference add http://m<N>.xc:8000 document-parse
#   ycluster inference add http://m<N>.xc:8000 mineru-parse
# Clients then hit https://inference.xc/v1/file_parse; the proxy
# rewrites the path to /file_parse and routes by the `model` form field.

set -euo pipefail

: "${MINERU_VENV:=$HOME/mineru-venv}"
: "${MINERU_PORT:=8000}"
: "${MINERU_HF_CACHE:=$HOME/mineru-models}"
: "${MINERU_LOG_FILE:=/opt/homebrew/var/log/mineru.log}"
: "${MINERU_MAX_CONCURRENT_REQUESTS:=3}"

MINERU_API="$MINERU_VENV/bin/mineru-api"

if [[ ! -x "$MINERU_API" ]]; then
    echo "Error: mineru-api not found at $MINERU_API" >&2
    echo "  uv pip install --python $MINERU_VENV/bin/python 'mineru[all]'" >&2
    exit 1
fi

mkdir -p "$MINERU_HF_CACHE" 2>/dev/null || true

export HF_HOME="$MINERU_HF_CACHE"
# Skip the network re-check on every boot — weights were pre-downloaded
# by setup-mineru.yml; subsequent restarts don't need to hit HF.
export MINERU_MODEL_SOURCE=local
export MINERU_API_MAX_CONCURRENT_REQUESTS="$MINERU_MAX_CONCURRENT_REQUESTS"

echo "Launching MinerU"
echo "  venv:    $MINERU_VENV"
echo "  port:    $MINERU_PORT"
echo "  cache:   $MINERU_HF_CACHE"
echo "  log:     $MINERU_LOG_FILE"
echo "  max-concurrent: $MINERU_API_MAX_CONCURRENT_REQUESTS"
echo "  API will listen on http://0.0.0.0:$MINERU_PORT"
echo

exec "$MINERU_API" \
    --host 0.0.0.0 \
    --port "$MINERU_PORT" \
    --enable-vlm-preload true \
    >> "$MINERU_LOG_FILE" 2>&1
