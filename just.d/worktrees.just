# Recruit isolated git worktrees for parallel branch work. Each worktree is
# a fresh checkout under ./trees/<branch> (gitignored) on a brand-new branch
# (`-b`). Tracked shared config (.codex/rules, .codex/skills, .codex/env,
# AGENTS.md, docs/, ...) comes along automatically from git; only the
# gitignored, personal files are symlinked back to this main checkout so the
# worktree is immediately usable (codex auth/config, direnv, venv, notes,
# tokens).
#
# We symlink individual ignored leaf files, never a tracked directory (.codex,
# .serena, .opencode are committed) — that would dirty the worktree's status
# with deletions. `config/ansible/host_vars` is likewise ignored, but it is a
# symlink in the main checkout; we chain the worktree's copy onto that symlink
# (`.../trees/<branch>/config/ansible/host_vars -> main/config/ansible/host_vars`)
# so there remains a single reference to the private ycluster-private/host_vars.

WORKTREE_ROOT := './trees'

# Recruit a linked worktree for a new branch under ./trees, sharing main's personal config.
worktree name:
    @mkdir -p {{WORKTREE_ROOT}}
    @git worktree add -b {{name}} {{WORKTREE_ROOT}}/{{name}}
    @srcs=".codex/config.toml AGENTS.local.md .envrc .venv .claude NOTES.md dev1.token dr.token"; \
    wt="{{WORKTREE_ROOT}}/{{name}}"; \
    root="$(pwd)"; \
    for src in $srcs; do \
        mkdir -p "$wt/$(dirname "$src")"; \
        link="$wt/$src"; \
        rel="$(realpath --relative-to "$(dirname "$link")" "$root/$src")"; \
        ln -sfn "$rel" "$link"; \
        echo "linked $link -> $rel"; \
    done; \
    host_vars="$wt/config/ansible/host_vars"; \
    mkdir -p "$(dirname "$host_vars")"; \
    rel="$(realpath --relative-to "$(dirname "$host_vars")" "$root/config/ansible")/host_vars"; \
    ln -sfn "$rel" "$host_vars"; \
    echo "linked $host_vars -> $rel"
    @echo "worktree ready: {{WORKTREE_ROOT}}/{{name}}"

# Remove a linked worktree (branch is kept; delete it separately if wanted).
remove-worktree name:
    git worktree remove {{WORKTREE_ROOT}}/{{name}}
