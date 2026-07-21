# Bash completion for the `inference` shell function in
# contrib/inference-completion.bash. Symlinks it into the user
# bash-completion dir (XDG default
# $HOME/.local/share/bash-completion/completions). Override with
# BASH_COMPLETION_USER_DIR.

COMPLETION_DIR := env_var_or_default('BASH_COMPLETION_USER_DIR', env('HOME') + '/.local/share/bash-completion/completions')
COMPLETION_SRC := absolute_path('contrib/inference-completion.bash')
COMPLETION_DST := COMPLETION_DIR + '/inference'

install-completions:
    @mkdir -p "{{COMPLETION_DIR}}"
    ln -sf "{{COMPLETION_SRC}}" "{{COMPLETION_DST}}"
    @echo "linked {{COMPLETION_DST}} -> {{COMPLETION_SRC}}"

uninstall-completions:
    @target="$(readlink {{COMPLETION_DST}} 2>/dev/null || true)"; \
    if [ "$target" = "{{COMPLETION_SRC}}" ]; then \
        rm -f {{COMPLETION_DST}} && echo "removed {{COMPLETION_DST}}"; \
    else \
        echo "{{COMPLETION_DST}} is not a symlink to {{COMPLETION_SRC}} (skipping)"; \
    fi
