# Bash completion for contrib/inference.
#
# Install (pick one):
#   source contrib/inference-completion.bash
#   cp contrib/inference-completion.bash /etc/bash_completion.d/inference
#
# Resolves the inference script relative to this file so it works regardless
# of cwd. When installed system-wide (no sibling `inference` binary), falls
# back to `inference` from PATH. Override _INFERENCE_CMD before sourcing to
# point elsewhere, e.g.:
#   _INFERENCE_CMD=inference source contrib/inference-completion.bash

_inference_completion_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${_INFERENCE_CMD:=${_inference_completion_dir}/inference}"
if [[ ! -x "$_INFERENCE_CMD" ]] && command -v inference >/dev/null 2>&1; then
    _INFERENCE_CMD=inference
fi

_INFERENCE_MODEL_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ycluster/inference-models"
_INFERENCE_MODEL_TTL=300  # 5 minutes

_inference_fresh_models() {
    local now mtime models
    now=$(date +%s)
    if [[ -f "$_INFERENCE_MODEL_CACHE" ]]; then
        mtime=$(stat -c %Y "$_INFERENCE_MODEL_CACHE" 2>/dev/null \
                || stat -f %m "$_INFERENCE_MODEL_CACHE" 2>/dev/null)
        if (( now - mtime < _INFERENCE_MODEL_TTL )); then
            cat "$_INFERENCE_MODEL_CACHE"
            return
        fi
    fi
    if models=$("$_INFERENCE_CMD" models 2>/dev/null) && [[ -n "$models" ]]; then
        mkdir -p "$(dirname "$_INFERENCE_MODEL_CACHE")" 2>/dev/null
        printf '%s\n' "$models" > "$_INFERENCE_MODEL_CACHE"
        printf '%s\n' "$models"
    fi
}

_inference_completion() {
    local cur prev words cword
    if declare -F _init_completion >/dev/null; then
        _init_completion || return
    else
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    fi

    if (( cword == 1 )); then
        COMPREPLY=( $(compgen -W "chat bench batch fairness models image smoke transcribe ocr" -- "$cur") )
        return
    fi

    case "${words[1]}" in
        chat)
            local models
            models="$(_inference_fresh_models)"
            COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            ;;
        bench)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--lens --gen --repeats --prompt --timeout --json -h --help" -- "$cur") )
            else
                local models
                models="$(_inference_fresh_models)"
                COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            fi
            ;;
        batch)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--concurrency --prompt-tokens --gen --cache --rounds --timeout --json -h --help" -- "$cur") )
            else
                local models
                models="$(_inference_fresh_models)"
                COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            fi
            ;;
        fairness)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--generations --prefills --generation-seconds --max-decode-tokens --prefill-tokens --min-prefill-seconds --context-tokens --prefill-settle-ms --repeats --min-chunks --min-decode-fraction --max-itl-p50-ratio --max-itl-p95-ratio --min-decode-tps-ratio --max-generation-spread --max-prefill-spread --no-baseline-after --timeout --json -h --help" -- "$cur") )
            else
                local models
                models="$(_inference_fresh_models)"
                COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            fi
            ;;
        image)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--max-tokens --timeout -h --help" -- "$cur") )
            else
                local models
                models="$(_inference_fresh_models)"
                COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            fi
            ;;
        smoke)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--max --json -h --help" -- "$cur") )
            else
                local models
                models="$(_inference_fresh_models)"
                COMPREPLY=( $(compgen -W "A B C $models" -- "$cur") )
            fi
            ;;
        transcribe)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--diarize --model --format -h --help" -- "$cur") )
            else
                local IFS=$'\n'
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            ;;
        ocr)
            if [[ $cur == -* ]]; then
                COMPREPLY=( $(compgen -W "--model --backend --format --output --timeout -h --help" -- "$cur") )
            else
                local IFS=$'\n'
                COMPREPLY=( $(compgen -f -- "$cur") )
            fi
            ;;
        models)
            COMPREPLY=()
            ;;
    esac
}

complete -F _inference_completion inference ./inference ./contrib/inference
