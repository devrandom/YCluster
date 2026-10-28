# Python dev environment. The venv at ./.venv holds ansible + dev
# tooling (see pyproject.toml); apt provides the runtime deps the
# cluster itself uses (etcd3, flask, …). `run-tests.sh` auto-uses
# ./.venv/bin/python when it's present.
#
# `setup-tools` is the same idea but for tools that should stay out of
# the project venv — they live as isolated `uv tool` installs in
# ~/.local/bin so the venv doesn't bloat with editor/agent extras.
#
# Run `setup-venv` once on a fresh checkout, and again after editing
# pyproject.toml or uv.lock. Run `setup-tools` once per machine.

# Create ./.venv and install the ycluster package editable so tests can
# import it directly from source. uv isn't always on PATH (the install
# script drops it in ~/.local/bin, which login shells add but
# non-login shells don't) — detect that and prepend it before failing.
[doc("Create ./.venv, install pinned deps from uv.lock, and install the ycluster package editable")]
setup-venv:
    #!/usr/bin/env bash
    set -euo pipefail
    unset VIRTUAL_ENV
    if ! command -v uv >/dev/null 2>&1; then
        if [ -x "$HOME/.local/bin/uv" ]; then
            export PATH="$HOME/.local/bin:$PATH"
            echo "uv not on PATH; using $HOME/.local/bin/uv"
        else
            echo "uv not found; install with 'pip install uv' or 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
            exit 1
        fi
    fi
    uv sync
    uv pip install --python .venv/bin/python -e config/ansible/admin/files/ycluster/

# Install ty and python-gitlab as isolated uv tools. ty is the current
# type checker per ty.toml — pyright is no longer used. `uv tool install`
# is idempotent — re-running this is a no-op for already-installed tools
# and upgrades them in place otherwise.
[doc("Install ty and python-gitlab as isolated uv tools")]
setup-tools:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v uv >/dev/null 2>&1; then
        if [ -x "$HOME/.local/bin/uv" ]; then
            export PATH="$HOME/.local/bin:$PATH"
            echo "uv not on PATH; using $HOME/.local/bin/uv"
        else
            echo "uv not found; install with 'pip install uv' or 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
            exit 1
        fi
    fi
    uv tool install ty
    uv tool install python-gitlab
