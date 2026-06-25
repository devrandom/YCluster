---
name: git-history-cleanup
description: Rewrite local git history (split, squash, reorder) before pushing — never rewrite commits others have already pulled
---

# Cleaning up git history before pushing

When local commits need restructuring (squash fixups, split a mixed
commit, reorder) and nothing has been pushed yet, do it with `git
reset --soft` rather than wrestling with `git rebase -i`. Quick,
scriptable, no editor.

## Workflow

1. **Confirm nothing's been pushed** yet for these commits:
   ```bash
   git log --oneline @{u}..
   ```
   If this is empty, the upstream already has them — don't rewrite
   without coordinating (force-push breaks anyone who's pulled).

2. **Pick the base** — the commit just before the cleanup starts:
   ```bash
   git log --oneline -10   # find the most recent "clean" commit
   ```

3. **Soft-reset to the base** — unstages everything but keeps the
   working tree intact (no data loss):
   ```bash
   git reset --soft <base>
   git status -s           # all the changes from the dropped commits, staged
   ```

4. **Re-stage by intent** into the new commit boundaries. Use
   `git reset HEAD <path>` to unstage specific paths back to the
   working tree:
   ```bash
   git reset HEAD path/to/file_a path/to/file_b
   git status -s           # now staged (first commit) vs working tree (next)
   ```

5. **Commit each boundary** in order:
   ```bash
   git commit -m "First logical change"
   git add <unstaged paths>
   git commit -m "Second logical change"
   ```

## Common cases

**Squash N fixups into one** — collapse a chain of "fixup" commits
into a single follow-up commit:
```bash
git reset --soft <base-before-fixups>
# all the files land staged; just commit once
git commit -m "Combined change"
```

**Split one commit into two** — when a single commit bundles two
logically separate changes:
```bash
git reset --soft <commit-before>
git reset HEAD path/to/second_concern
git commit -m "First concern"
git add path/to/second_concern
git commit -m "Second concern"
```

**Reorder** — soft-reset past the boundary, then commit in the new
order.

## Gotchas

- **Never `git reset --hard` to "clean up"** — it deletes uncommitted
  work. Soft keeps the working tree intact.
- **`git reset --soft` after `git add` of new files** — the new
  files come back staged (good), but untracked files stay
  untracked.
- **If you already pushed**, use `git push --force-with-lease`
  instead of `--force` (the lease variant refuses to overwrite if
  someone else has pushed in the meantime). Coordinate with anyone
  who's pulled — they need to `git pull --rebase` or reset.
- **Author/committer dates** survive soft-reset unchanged. If you
  care about commit chronology, use `git rebase --committer-date-is-author-date`
  or similar after.
- **Pre-commit hooks** will rerun on each new commit. If a hook
  blocks, fix the underlying issue rather than `--no-verify`-ing
  past it.
