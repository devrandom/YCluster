#!/usr/bin/env bash
# MinerU (opendatalab/MinerU 3.4.x) launcher for Linux/Nvidia cluster
# nodes (nv*). Run by the mineru systemd unit (as user `dev`) — see
# config/ansible/admin/install-mineru.yml. Can also be run by hand
# under `dev` to watch the startup banner and CUDA engine init.
#
# Prerequisites on the host (all provisioned by install-mineru.yml):
#   - uv at /snap/bin/uv
#   - mineru[all] installed into /home/dev/mineru-venv
#   - HF cache exists and is dev-writable (defaults to ~/.cache/huggingface)
#   - mineru-models-download has been run once (populates the cache)
#
# Listens on :8000 with no API key — cluster-internal auth is handled
# upstream by local-ai-proxy + nginx (Open-WebUI api_key table).
# Register this node as a local-ai-proxy backend with:
#   ycluster inference add http://nv<N>.xc:8000 document-parse
#   ycluster inference add http://nv<N>.xc:8000 mineru-parse
# Clients then hit https://inference.xc/v1/file_parse; the proxy
# rewrites the path to /file_parse and routes by the `model` form field.

set -euo pipefail

: "${MINERU_VENV:=/home/dev/mineru-venv}"
: "${MINERU_PORT:=8000}"
: "${MINERU_HF_CACHE:=/home/dev/.cache/huggingface}"
: "${MINERU_LOG_FILE:=/var/log/mineru.log}"
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
# by install-mineru.yml; subsequent restarts don't need to hit HF.
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

# The systemd unit already redirects stdout/stderr to the journal; we
# also tee to $MINERU_LOG_FILE so operators can grep without journalctl.
exec "$MINERU_API" \
    --host 0.0.0.0 \
    --port "$MINERU_PORT" \
    --enable-vlm-preload true \
    >> "$MINERU_LOG_FILE" 2>&1
