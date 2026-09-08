---
name: git-ssh
description: Handle Git SSH fetch/push failures in the sandbox — when the error is SSH config access, request escalated execution rather than changing repo or SSH configuration.
---

# Git over SSH in the sandbox

Git SSH operations run through the host `ssh` client, which may fail before
contacting the remote. Treat this failure shape as sandbox-only:

```text
Bad owner or permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf
fatal: Could not read from the remote repository.
```

Do **not** fix this by editing `/etc/ssh`, changing repo remotes, or weakening
SSH config checks. Instead, request escalated execution for the specific Git
command that needs remote access, such as `git fetch` or `git push`.

Before escalating, verify the repo is otherwise ready: the branch is correct,
unrelated work is not accidentally staged, and the commit hygiene scan has
passed.
