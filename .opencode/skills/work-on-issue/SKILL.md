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

## Shared resources

- Worktree-local files (including `justfile` and `just.d/`) are private to
  that tree. Do not commit build/tooling changes unless the issue requires
  them.
- Only the orchestrator (main checkout) may edit repo-wide shared state such
  as `NOTES.md` or the todo index, or coordinate branches and merge order.
- Each worktree has its own `.venv` and `tmp/`; never point a worktree at the
  main checkout's `.venv`.
- If `uv` fails with a snap "transient scope" error, bootstrap with
  `/snap/astral-uv/current/bin/uv sync` and
  `/snap/astral-uv/current/bin/uv pip install --python .venv/bin/python -e
  config/ansible/admin/files/ycluster/`.
- Multiple agent worktrees may run concurrently: use the worktree's own
  branch, never commit or push from another agent's tree, and keep changes
  inside the assigned worktree.

## Verification constraints

Do not run dev-cluster system tests (`just test-system`,
`just test-system-quick`, or `just test-system-fresh`) unless explicitly
asked; they require the shared dev cluster and are not part of the agent
unit-test flow.

## Baseline gates

From the worktree, run both gates **before** making changes:

```bash
just check
just metrics
```

Record the baseline. If `just metrics` fails before implementation starts,
the failure is pre-existing debt on the base branch: record it in the MR
description and proceed. The stop-gate applies **before merging to main**
(MR review), not before performing work. The implementation must add zero new
violations relative to the base branch. Do not fix unrelated debt and do not
adopt a new baseline just to unblock the issue.

## Implement

Read the issue file and nearby code, then make the smallest change that
satisfies the issue. Preserve existing patterns and avoid unrelated cleanup.

## Verify

After implementing, rerun both gates from the worktree:

```bash
just check
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
issue/todo path when applicable and verification results from `just check`
and `just metrics`.

After the MR is created, stop. Do not merge it, monitor CI, or make further
changes unless the orchestrator asks for follow-up.

## Orchestrator workflow (main agent only)

The main agent may run this skill for itself (single issue) or act as the
orchestrator when the user asks for multiple issues or "spin off agents".
Orchestrating is not delegation of judgment — the main agent remains
responsible for queue state, batch composition, and final verification.

### Batching

- Keep at most 4 subagents in flight; as soon as one finishes and the
  orchestrator has recorded its result, spawn the next queued issue. Do not
  wait for the whole batch to drain.
- Choose issues for a batch that touch distinct surfaces (e.g. don't put two
  admin-api Python changes and two Ansible changes in the same batch) to
  reduce merge collisions and shared-file churn.
- Pick issues in the priority order shown in `todo/INDEX.md` unless the user
  says otherwise.

### Queue state

- Track batches in `NOTES.md` under "Agent todo queue" with checkboxes and
  MR numbers: in-flight, in-review, and done sections.
- After each batch completes, update the queue before spawning the next one.
- The main agent never merges MRs; merging is always the user's action. Move
  an issue to "done" only after the user has merged it.

### Waiting and wake-ups

- After spawning a batch, wait with the longest available agent-wait call;
  do not poll with short timeouts. Subagent completions are also delivered
  automatically between user turns.
- When a completion notification arrives: record the MR/commit in the queue,
  spot-check the worktree state (clean tree, commit pushed), and continue
  waiting for the rest of the batch.
- If a subagent stalls or fails a gate, stop it only after the user is
  informed; prefer reporting the blocker over silent retries.

### Review pass

- After each implementation subagent finishes and publishes its MR, spawn a
  separate review subagent using the `review-agent` skill. Give it the MR
  iid, todo path, worktree path, and branch.
- Don't hand the reviewer a diff file. Have it open the worktree (branch
  `<slug>` checked out at `work/<slug>`) and review the changes with respect
  to the base branch (e.g. `main`), e.g. via `git diff <base>...HEAD` inside
  the worktree.
- Frame the request as a fresh, independent evaluation of the changes on
  their own merits; do not mention prior review history or summarize what
  earlier reviewers found. Prior-review framing anchors the reviewer to the
  same conclusions and areas the earlier pass covered.
- When the user says "spawn a subagent to review" (or similar), treat that as
  an instruction to the orchestrator, not as the reviewer's prompt. Compose a
  self-contained task description for the reviewer: what to read, what to
  evaluate, and what to report. Never relay the user's meta-instruction
  verbatim — the subagent may try to spawn its own nested agent instead of
  doing the review itself.
- Ask the reviewer to identify "goodhearting" of test coverage: tests or
  assertions that make coverage metrics look better without really improving
  resilience (e.g. testing implementation details, over-mocking, trivially
  passing tests, or duplicate variants that don't exercise new behavior).
- Wait for the review verdict before treating the issue as ready for the
  user. On approval, move the issue to in-review with its MR number. On
  "request changes", send the findings back to the implementation subagent
  or report the blocker if that agent is no longer available.
- Do not have the reviewer implement fixes; keep review and implementation
  separate so the second pass stays independent.

### Verification of subagent work

- The main agent never merges MRs; merging is always the user's action. Check
  the worktree diff, branch cleanliness, and MR state, then move the issue to
  in-review for the user to merge.
- When the user asks to review a batch, verify each MR: CI status, the diff
  matches the todo scope, and no unrelated files were touched. The user
  performs the merge.
