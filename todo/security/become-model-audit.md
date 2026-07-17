# Reconsider become usage in playbooks

Prompted by two macOS findings (incident:
`../ycluster-private/incidents/2026-06-13 — macOS become findings
(provisioning m3, first wg-joined Mac)`).

## Finding 1: `raw` + `become` cannot take a multiline command

Isolated on m3: trivial+become works, `if/fi` on one physical line +
become works, but a real-newline (YAML `|` block) fails with
`/bin/sh: -c: line 1: syntax error: unexpected end of file`. The `-i`
login-shell flag in `group_vars/macos.yml`
(`ansible_become_flags: "-H -i"`) is *not* the cause.

So any future `raw` task under become must be a single physical line
(folded `>-` scalar, `||`-grouped, no embedded single quotes).

## Finding 2: macOS become model is muddled

The macOS play's become model is muddled: `become: yes` at play level
but `become_user: dev` for Homebrew tasks, assuming brew at
`/opt/homebrew` owned by `dev` — true only when the playbook installed
brew. On a wg-joined Mac the human pre-installs brew under their admin
account, so the clone+chown tasks skip and `brew install` as `dev`
fails ("directories are not writable by your user").

## Decide the intended ownership/become model

Options:

- Normalize brew ownership to `dev` regardless of who installed it (a
  chown task that runs even when brew pre-exists)
- Run brew as whatever account owns the prefix
- Standardize brew installation so the paths converge

Audit `become`/`become_user`/`-i` usage globally while here.
