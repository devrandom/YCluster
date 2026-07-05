#!/usr/bin/env bash
# TEI (text-embeddings-inference) launcher for macOS cluster nodes (m1+).
# Run by the com.ycluster.tei LaunchDaemon (as user `dev`) — see
# config/ansible/macos/setup-tei.yml. Can also be run by hand under
# `dev` to watch the startup banner and Metal init.
#
# Prerequisites on the mac (all provisioned by setup-tei.yml):
#   - text-embeddings-inference installed (brew install)
#   - HF cache directory TEI_HF_CACHE exists and is dev-writable
#
# Listens on :8080 with no API key — cluster-internal auth is handled
# upstream by local-ai-proxy + nginx (Open-WebUI api_key table).
# Register this node as a local-ai-proxy backend with:
#   ycluster inference add http://m<N>.xc:8080 BAAI/bge-m3
# Clients then hit https://inference.xc/v1/embeddings as for any other
# model; the proxy routes by the model field in the body.

set -euo pipefail

: "${TEI_MODEL_ID:=BAAI/bge-m3}"
: "${TEI_PORT:=8080}"
: "${TEI_HF_CACHE:=$HOME/tei-models}"
: "${TEI_LOG_FILE:=/opt/homebrew/var/log/tei.log}"

if [[ ! -x /opt/homebrew/bin/text-embeddings-router ]]; then
    echo "Error: text-embeddings-router not found at /opt/homebrew/bin/" >&2
    echo "  brew install text-embeddings-inference" >&2
    exit 1
fi

mkdir -p "$TEI_HF_CACHE" 2>/dev/null || true

echo "Launching TEI"
echo "  model:   $TEI_MODEL_ID"
echo "  port:    $TEI_PORT"
echo "  cache:   $TEI_HF_CACHE"
echo "  log:     $TEI_LOG_FILE"
echo "  API will listen on http://0.0.0.0:$TEI_PORT"
echo

exec /opt/homebrew/bin/text-embeddings-router \
    --model-id "$TEI_MODEL_ID" \
    --port "$TEI_PORT" \
    --hostname 0.0.0.0 \
    --huggingface-hub-cache "$TEI_HF_CACHE" \
    >> "$TEI_LOG_FILE" 2>&1
