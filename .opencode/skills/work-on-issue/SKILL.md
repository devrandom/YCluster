---
name: work-on-issue
description: Implement a YCluster issue in an isolated worktree with test and quality-metric baselines, then publish the result as a GitLab merge request. Use when asked to work on or implement a todo issue, or to open an MR for a change.
---

# Work on an issue

## Worktree

Use the slug-aware worktree recipe. For a tracked issue, pass its todo file;
for an ad hoc task, pass the chosen slug:

```bash
just agent-worktree todo/code-quality/go-health-probe-context.md
just agent-worktree <chosen-slug>
```

Work inside `work/<slug>` on branch `<slug>`. The recipe creates a dedicated
`.venv`; do not use the main checkout's environment for worktree tests.

## Baseline gates

From the worktree, run both gates **before** making changes:

```bash
just test
just metrics
```

Record a clean baseline. If metrics fail before implementation starts, that is
a pre-existing violation: stop and surface it. Do not fix unrelated debt and
do not adopt a new baseline just to unblock the issue.

## Implement

Read the issue file and nearby code, then make the smallest change that
satisfies the issue. Preserve existing patterns and avoid unrelated cleanup.

## Verify

After implementing, rerun both gates from the worktree:

```bash
just test
just metrics
```

Also run `git diff --check`. If the change touches goroutines, timers, shared
state, or other concurrency seams, run `make -C local-ai-proxy test-race`.

Fix failures in the issue's scope before publishing. If a metrics change is
intentional cleanup, run `just metrics-adopt`, review the baseline diff, and
include `scripts/metrics/baseline.json` in the change. Never adopt a baseline
merely to make a failing gate pass.

## GitLab merge request

Review the full diff before publishing. Follow `commit-hygiene` for leak
scanning and commit messages. A request for this workflow is authorization to
create the commits needed to publish the merge request, but not to commit
unrelated work.

Push the branch to `origin`, then create a GitLab merge request targeting
`main` with the `gitlab` CLI:

```bash
gitlab -o json -f iid,title,web_url project-merge-request create \
  --project-id devrandom01/ycluster \
  --source-branch <slug> \
  --target-branch main \
  --title "<compact imperative title>" \
  --description "<what/why, issue path, and verification results>"
```

Do not URL-encode the `/` in the project path for this CLI; use
`devrandom01/ycluster` directly. The MR description should include the
issue/todo path when applicable and verification results from `just test`
and `just metrics`.
