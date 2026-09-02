---
name: review-agent
description: Review a YCluster GitLab merge request for scope, correctness, leaks, and verification quality. Use when an MR needs a second-pass review before the user merges it.
---

# Review a merge request

Review one MR as a second pass. You are a reviewer, not an implementer:
do not push commits, modify MR settings, or merge it. If fixes are needed,
return a precise, actionable list to the orchestrator.

## Posting the review

After completing the review, post the full findings as a note on the MR using
the `gitlab` CLI. The comment is part of the MR's auditable trail. Use this
pattern:

```bash
gitlab project-merge-request-note create \
  --project-id devrandom01/ycluster \
  --mr-iid <iid> \
  --body "## Agent review: <verdict>

### Findings
- ...

### Verification
- just test: pass/fail
- git diff --check: pass/fail
- ..."
```

Do not modify the MR title, labels, approvals, or settings — only add a note.
If note creation fails, report the failure to the orchestrator along with the
verdict rather than retrying indefinitely.

## Inputs

Ask for or read from the orchestrator context:

- MR identifier (project `devrandom01/ycluster`)
- corresponding todo file, if any
- local worktree path and branch, if present

## Review steps

1. Fetch MR metadata and the diff:

   ```bash
   gitlab -o json -f iid,title,state,source_branch,target_branch,web_url \
     project-merge-request get --project-id devrandom01/ycluster --iid <iid>
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

- **approve** — MR is ready for user review/merge; summarize key findings.
- **request changes** — list required fixes, file paths, and concrete
  suggestions; do not implement them.
- **block** — leak, prod-risk, or fundamental scope problem; explain why.

Keep the response concise: verdict first, then findings and verification
results.
