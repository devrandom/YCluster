---
name: review-agent
description: Review a YCluster Forgejo pull request for scope, correctness, leaks, and verification quality. Use when a PR needs a second-pass review before the user merges it.
---

# Review a pull request

Review one PR as a second pass. You are a reviewer, not an implementer:
do not push commits, modify PR settings, or merge it. If fixes are needed,
return a precise, actionable list to the orchestrator.

## Posting the review

After completing the review, post the full findings as a comment on the PR
using the `tea` CLI. The comment is part of the PR's auditable trail. Use
this pattern:

```bash
tea comments add --login YCluster --repo devrandom/ycluster <pr-number> \
  "## Agent review: <verdict>

### Findings
- ...

### Verification
- just test: pass/fail
- git diff --check: pass/fail
- ..."
```

Do not modify the PR title, labels, approvals, or settings — only add a
comment. If comment creation fails, report the failure to the orchestrator
along with the verdict rather than retrying indefinitely.

## Inputs

Ask for or read from the orchestrator context:

- PR identifier (repo `devrandom/ycluster`)
- corresponding todo file, if any
- local worktree path and branch, if present

## Review steps

1. Fetch PR metadata and the diff:

   ```bash
   tea pulls <number> --login YCluster --repo devrandom/ycluster
   git fetch origin <source-branch>
   git diff origin/main...FETCH_HEAD
   ```

2. Check scope first: every change should trace to the todo file or explicit
   task. Flag unrelated refactors, formatting-only churn, build/tooling
   changes, and feature creep.
3. Read the implementation and tests at the diff level. Prefer correctness,
   error handling, security/leak risks, and maintainability over style
   nits already covered by formatters.
4. Verify the claimed checks by rerunning the narrowest useful subset in the
   worktree: at minimum `just test` and `git diff --check`; add
   `just metrics` when tests or coverage-sensitive code changed, and
   `make -C local-ai-proxy test-race` for concurrency changes.
5. Follow `commit-hygiene` for the public-repo leak rules and scan the full
   diff, not just the staged delta.

## Verdict

Return one of:

- **approve** — PR is ready for user review/merge; summarize key findings.
- **request changes** — list required fixes, file paths, and concrete
  suggestions; do not implement them.
- **block** — leak, prod-risk, or fundamental scope problem; explain why.

Keep the response concise: verdict first, then findings and verification
results.
