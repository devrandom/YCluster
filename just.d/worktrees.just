# Recruit isolated git worktrees for parallel branch work. Each worktree is
# a fresh checkout under ./work/<branch> (gitignored) on a brand-new branch
# (`-b`). Tracked shared config (.codex/rules, .codex/skills, .codex/env,
# AGENTS.md, docs/, ...) comes along automatically from git; only the
# gitignored, personal files are symlinked back to this main checkout so the
# worktree is immediately usable (codex auth, direnv, venv, notes, tokens).
#
# We symlink individual ignored leaf files, never a tracked directory (.codex,
# .opencode are committed) — that would dirty the worktree's status
# with deletions. The one exception is .codex/config.toml: it must NOT be a
# symlink. Codex resolves a linked worktree's .git pointer and marks the
# resolved gitdir (~/.git/worktrees/<name>) read-only by default, which beats
# the broader `~/proj/ycluster/.git` writable root — so git operations fail in
# the worktree. dev/worktree-config.py instead generates a per-worktree
# config.toml from the main one, registering the exact gitdir as a writable
# root to suppress that carveout. `config/ansible/host_vars` is likewise
# ignored, but it is a symlink in the main checkout; we chain the worktree's
# copy onto that symlink
# (`.../work/<branch>/config/ansible/host_vars -> main/config/ansible/host_vars`)
# so there remains a single reference to the private ycluster-private/host_vars.

WORKTREE_ROOT := './work'

# Recruit a linked worktree for a new branch under ./work, sharing main's personal config.
# Idempotent: if the worktree is already registered, it skips `git worktree add` and just
# refreshes the generated config + symlinks (nothing is removed, no changes are lost).
# Set shared_venv to 0 to leave .venv unlinked for `agent-worktree` to create independently.
worktree name shared_venv='1':
    @mkdir -p {{WORKTREE_ROOT}}
    @wt="{{WORKTREE_ROOT}}/{{name}}"; \
    root="$(pwd)"; \
    wt_abs="$root/${wt#./}"; \
    if git worktree list --porcelain | grep -q "^worktree $wt_abs$"; then \
        echo "worktree {{name}} already exists; skipping add"; \
    else \
        git worktree add -b {{name}} {{WORKTREE_ROOT}}/{{name}}; \
    fi; \
    python3 dev/worktree-config.py "$root" "$wt_abs"; \
    srcs="AGENTS.local.md .envrc .claude NOTES.md dev1.token dr.token"; \
    if [ "{{shared_venv}}" = "1" ]; then \
        srcs=".venv $srcs"; \
    fi; \
    for src in $srcs; do \
        mkdir -p "$wt/$(dirname "$src")"; \
        link="$wt/$src"; \
        rel="$(realpath --relative-to "$(dirname "$link")" "$root/$src")"; \
        ln -sfn "$rel" "$link"; \
        echo "linked $link -> $rel"; \
    done; \
    host_vars="$wt/config/ansible/host_vars"; \
    mkdir -p "$wt/config/ansible"; \
    rel="$(realpath --relative-to "$(dirname "$host_vars")" "$root/config/ansible")/host_vars"; \
    ln -sfn "$rel" "$host_vars"; \
    echo "linked $host_vars -> $rel"; \
    mkdir -p "$wt/tmp" "$root/tmp/uv-cache" "$root/tmp/gopath" "$root/tmp/go-cache" "$root/tmp/gotmp"; \
    rel_uv="$(realpath --relative-to "$wt/tmp" "$root/tmp/uv-cache")"; \
    if [ -d "$wt/tmp/uv-cache" ] && [ ! -L "$wt/tmp/uv-cache" ]; then \
        rm -rf "$wt/tmp/uv-cache"; \
    fi; \
    ln -sfn "$rel_uv" "$wt/tmp/uv-cache"; \
    echo "linked $wt/tmp/uv-cache -> $rel_uv (shared uv cache)"; \
    for d in gopath go-cache gotmp; do \
        if [ -d "$wt/tmp/$d" ] && [ ! -L "$wt/tmp/$d" ]; then \
            chmod -R u+w "$wt/tmp/$d" 2>/dev/null || true; \
            rm -rf "$wt/tmp/$d"; \
        fi; \
        rel_d="$(realpath --relative-to "$wt/tmp" "$root/tmp/$d")"; \
        ln -sfn "$rel_d" "$wt/tmp/$d"; \
        echo "linked $wt/tmp/$d -> $rel_d (shared Go cache)"; \
    done
    @echo "worktree ready: {{WORKTREE_ROOT}}/{{name}}"

# Create a decoupled agent worktree. Pass either a todo file (for example
# todo/code-quality/foo.md) or a slug chosen for a task not tracked in todo/.
# The worktree and branch use the sanitized slug; a dedicated .venv is created
# inside the tree so agent tasks cannot mutate the main checkout's environment.
[doc("Create an isolated worktree + venv from a todo filename or chosen slug")]
agent-worktree target:
    #!/usr/bin/env bash
    set -euo pipefail
    target="{{target}}"
    case "$target" in
        *.md)
            if [ ! -f "$target" ]; then
                echo "todo file not found: $target" >&2
                exit 1
            fi
            slug="$(basename "$target" .md)"
            ;;
        *)
            slug="$target"
            ;;
    esac
    slug="$(printf '%s' "$slug" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[-._]+//; s/[-._]+$//')"
    if [ -z "$slug" ]; then
        echo "could not derive a non-empty slug from: $target" >&2
        exit 1
    fi
    just worktree "$slug" 0
    just --justfile {{justfile()}} --working-directory "{{WORKTREE_ROOT}}/$slug" setup-venv
    echo "agent worktree ready: {{WORKTREE_ROOT}}/$slug"

# Remove a linked worktree (branch is kept; delete it separately if wanted).
remove-worktree name:
    git worktree remove {{WORKTREE_ROOT}}/{{name}}
