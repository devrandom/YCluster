#!/usr/bin/env bash
# Wrapper around the dark-devotion image's baked serve-minimax-m3-nvfp4.sh that
# overrides its hard-pinned served model name.
#
# The vendor script pins SERVED_MODEL_NAME="MiniMax-M3-NVFP4" (not env-driven)
# and its arg loop *rejects* a --served-model-name override. To keep the
# established `brandonmusic/minimax-m3-nvfp4` gateway id (the proxy forwards the
# client's model id verbatim - it has no backend-side rename), we patch the
# vendor script's two relevant lines in place. The container filesystem is a
# writable overlay, so the edit is per-instance and re-applied on every launch
# (the image ships the script unpatched). vLLM's --served-model-name accepts
# multiple values, so we serve a space-separated list: the established id plus
# the image's own MiniMax-M3-NVFP4, for a seamless cutover.
#
# Fails loudly if either target line is absent (e.g. a future image rev moved
# them), so we never silently serve the wrong id.
set -euo pipefail

VENDOR="${VENDOR:-/usr/local/bin/serve-minimax-m3-nvfp4.sh}"
NAMES="${SERVED_MODEL_NAMES:-brandonmusic/minimax-m3-nvfp4 MiniMax-M3-NVFP4}"

pin_line='SERVED_MODEL_NAME="MiniMax-M3-NVFP4"'
exec_flag='--served-model-name "${SERVED_MODEL_NAME}"'

grep -qF "$pin_line" "$VENDOR" \
  || { echo "serve-minimax-m3-named: pinned-name line not in $VENDOR; refusing (image changed?)" >&2; exit 1; }
grep -qF -- "$exec_flag" "$VENDOR" \
  || { echo "serve-minimax-m3-named: served-name flag not in $VENDOR; refusing (image changed?)" >&2; exit 1; }

# 1) replace the pinned scalar with our list; 2) unquote the flag so the list
#    expands into multiple --served-model-name values, and append ${EXTRA_VLLM_ARGS}
#    right after it - an inert hook for injecting extra vllm flags (e.g. the EAGLE3
#    --speculative-config) from the start script, without needing a second anchor
#    in the vendor recipe. The :- keeps it safe under the recipe's set -u when the
#    env is unset (the default), so normal launches are byte-for-byte unchanged.
sed -i "s|^${pin_line}\$|SERVED_MODEL_NAME=\"${NAMES}\"|" "$VENDOR"
sed -i 's|--served-model-name "${SERVED_MODEL_NAME}"|--served-model-name ${SERVED_MODEL_NAME} ${EXTRA_VLLM_ARGS:-}|' "$VENDOR"

exec "$VENDOR" "$@"
