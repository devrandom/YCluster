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

# Install serena-agent, ty, the local mcp-fstools sibling, and
# python-gitlab as isolated uv tools. serena is pinned to its git source
# (matches README.md Tools section); ty is the current type checker per
# ty.toml — pyright is no longer used; mcp-fstools is the sibling repo
# that opencode registers as a remote MCP. `uv tool install` is
# idempotent — re-running this is a no-op for already-installed tools
# and upgrades them in place otherwise.
#
# mcp-fstools is expected as a sibling repo (../mcp-fstools). If you
# don't have it locally, clone it from github.com/devrandom/mcp-fstools
# next to ycluster — the install step warns and skips rather than
# failing so the other tools still install.
[doc("Install serena-agent (from git), ty, python-gitlab, and ../mcp-fstools (skipped if sibling missing) as isolated uv tools")]
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
    uv tool install --from git+https://github.com/oraios/serena serena-agent
    uv tool install ty
    uv tool install python-gitlab
    fstools_dir="{{justfile_directory()}}/../mcp-fstools"
    if [ -d "$fstools_dir" ]; then
        uv tool install "$fstools_dir"
    else
        echo "skipping mcp-fstools: sibling ../mcp-fstools not found" >&2
        echo "  clone it next to ycluster: git clone https://github.com/devrandom/mcp-fstools.git ../mcp-fstools" >&2
        echo "  then re-run 'just setup-tools' to install it" >&2
    fi

