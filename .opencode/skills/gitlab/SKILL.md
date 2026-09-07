---
name: gitlab
description: Interact with GitLab MRs, issues, branches, and CI using the `gitlab` CLI (python-gitlab). Use when asked to close/review/create MRs, list issues, or query GitLab for this project.
---

# GitLab

Use the `gitlab` CLI (`~/.local/bin/gitlab`, python-gitlab wrapper).
Configuration lives in `~/.python-gitlab.cfg` under the `ycluster` profile;
invoke with `gitlab -g ycluster`.

Project: `devrandom01/ycluster`, project ID `83564615`.

## Common operations

### Merge requests

```bash
# Get MR details (state, title, source branch)
gitlab -g ycluster -o json project-merge-request get --project-id 83564615 --iid <N>

# Close an MR
gitlab -g ycluster -o json project-merge-request update --project-id 83564615 --iid <N> --state-event close

# List open MRs
gitlab -g ycluster -o json project-merge-request list --project-id 83564615 --state opened
```

### Branches

```bash
# Delete a remote branch
gitlab -g ycluster -o json project-branch delete --project-id 83564615 --name <branch>
```

### Notes

- `-o json` output goes to stdout as a JSON array; pipe through `python3 -c` to extract fields.
- Some subcommands reject `-o json` after the resource name; put global flags before the subcommand.
- The output can be very large for `list` calls; use `--search` or pipe through a filter to limit noise.
- Never commit, echo, or log the private token from `~/.python-gitlab.cfg`.
- Git push/pull over SSH may fail in the sandbox (network or SSH config restrictions). If so, request escalation for the `git push`/`git pull` command.
