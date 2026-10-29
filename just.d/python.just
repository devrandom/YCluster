# Python dev environment. The venv at ./.venv holds ansible + dev
# tooling (see pyproject.toml); apt provides the runtime deps the
# cluster itself uses (etcd3, flask, …). `run-tests.sh` auto-uses
# ./.venv/bin/python when it's present.
#
# `setup-tools` is the same idea but for tools that should stay out of
# the project venv — uv tools live in ~/.local/bin and Go tools in
# GOPATH/bin so the venv doesn't bloat with editor/agent extras.
#
# Run `setup-venv` once on a fresh checkout, and again after editing
# pyproject.toml or uv.lock. Run `setup-tools` once per machine; it
# installs missing tools only, and `just setup-tools -f` reinstalls all
# of them.

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

# Per-tool installs skip when the binary is already present; pass -f to
# reinstall (`just setup-tools -f` or `just setup-tools-ty -f`). ty is the
# current type checker per ty.toml — pyright is no longer used.

[doc("Install ty as an isolated uv tool (skips if present; -f to reinstall)")]
setup-tools-ty force='':
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{force}}" ] && { command -v ty >/dev/null 2>&1 || [ -x "$HOME/.local/bin/ty" ]; }; then
        echo "ty already installed; skipping (pass -f to reinstall)"
        exit 0
    fi
    uv="$(command -v uv || true)"
    uv="${uv:-$HOME/.local/bin/uv}"
    if [ ! -x "$uv" ]; then
        echo "uv not found; install with 'pip install uv' or 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
        exit 1
    fi
    "$uv" tool install --force ty

[doc("Install python-gitlab as an isolated uv tool (skips if present; -f to reinstall)")]
setup-tools-python-gitlab force='':
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{force}}" ] && { command -v gitlab >/dev/null 2>&1 || [ -x "$HOME/.local/bin/gitlab" ]; }; then
        echo "python-gitlab already installed; skipping (pass -f to reinstall)"
        exit 0
    fi
    uv="$(command -v uv || true)"
    uv="${uv:-$HOME/.local/bin/uv}"
    if [ ! -x "$uv" ]; then
        echo "uv not found; install with 'pip install uv' or 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
        exit 1
    fi
    "$uv" tool install --force python-gitlab

[doc("Install tea at the pinned release commit (skips if present; -f to reinstall)")]
setup-tools-tea force='':
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{force}}" ]; then
        if command -v tea >/dev/null 2>&1; then
            echo "tea already installed; skipping (pass -f to reinstall)"
            exit 0
        fi
        if command -v go >/dev/null 2>&1 && [ -x "$(go env GOPATH)/bin/tea" ]; then
            echo "tea already installed; skipping (pass -f to reinstall)"
            exit 0
        fi
    fi

    # tea (Gitea/Forgejo CLI), pinned to the v0.16.0 release commit.
    # Requires go >= 1.26 (auto-switched via GOTOOLCHAIN=auto).
    # -f reinstalls the pinned commit; bump the hash to upgrade.
    if ! command -v go >/dev/null 2>&1; then
        echo "go not found; install go to get tea" >&2
        exit 1
    fi
    go install gitea.dev/tea@9c12138d6273ba8feb6e7578833cc330c0cd6461

[doc("Build veans from the pinned source tag (skips if present; -f to reinstall)")]
setup-tools-veans force='':
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{force}}" ]; then
        if command -v veans >/dev/null 2>&1; then
            echo "veans already installed; skipping (pass -f to reinstall)"
            exit 0
        fi
        if command -v go >/dev/null 2>&1 && [ -x "$(go env GOPATH)/bin/veans" ]; then
            echo "veans already installed; skipping (pass -f to reinstall)"
            exit 0
        fi
    fi

    if ! command -v go >/dev/null 2>&1; then
        echo "go not found; install go to get veans" >&2
        exit 1
    fi

    # veans (Vikunja CLI), built from the source tag matching the deployed
    # server in install-vikunja.yml. veans is a submodule with its own go.mod
    # and no module tag, so `go install code.vikunja.io/veans@...` cannot
    # resolve it; build the tagged source instead of downloading a prebuilt
    # release binary. Requires go >= 1.27 (auto-switched via GOTOOLCHAIN=auto).
    veans_version=v2.7.1
    veans_commit=5619c4a9f66ddfc13f7bbba24ada07c3e82ae7a0
    veans_src="${YC_TMP_ROOT:-$(git rev-parse --show-toplevel)/tmp}/veans-src"
    if [ ! -d "$veans_src/.git" ]; then
        git clone --quiet --depth 1 --branch "$veans_version" \
            https://github.com/go-vikunja/vikunja.git "$veans_src"
    fi
    if [ "$(git -C "$veans_src" rev-parse HEAD)" != "$veans_commit" ]; then
        git -C "$veans_src" fetch --quiet --depth 1 origin "$veans_commit"
        git -C "$veans_src" checkout --quiet --detach "$veans_commit"
    fi
    go_bin="$(go env GOPATH)/bin"
    mkdir -p "$go_bin"
    (
        cd "$veans_src/veans"
        GOTOOLCHAIN=auto go build -trimpath \
            -ldflags "-X main.version=$veans_version" \
            -o "$go_bin/veans" ./cmd/veans
    )

[doc("Install missing dev tools (ty, python-gitlab, tea, veans); -f to reinstall all")]
setup-tools force='': (setup-tools-ty force) (setup-tools-python-gitlab force) (setup-tools-tea force) (setup-tools-veans force)
