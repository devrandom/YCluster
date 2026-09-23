# AGENTS.md

This file orients agents working in this repo. It is deliberately lean;
detailed knowledge lives in `docs/` and the opencode skills.

## Overview

YCluster is a self-bootstrapping infrastructure platform for small AI
clusters: it provisions bare metal servers via PXE boot, manages distributed
storage with MicroCeph, and orchestrates services with etcd-based leader
election.

## Hard rules

- **Never leak production data into the repo**: no emails, deployment
  URLs/hostnames/public IPs, user-chosen VM names, dates of production
  events, or hostnames with index > 3 (e.g. c4, s5). Track deployment-specific
  tasks in `NOTES.md` (gitignored).
- **No prebuilt third-party binaries.** Software comes from apt, held snaps,
  source builds in a `build-<name>.yml` playbook (vendored under `/opt/src/`,
  pinned to a git SHA), or in-cluster Docker images. Never
  `get_url`/`curl`/`wget` compiled artifacts, PyPI C-extension wheels, or
  `curl | bash`; add a build playbook instead. See `docs/CONTRIBUTING.md` and
  `docs/operations/packages.md`.
- **Don't change prod security or stability (e.g. firewall) and don't reboot
  any prod machine without checking with the user first.** Avoid doing things
  manually in prod when there's a best-practice way (e.g. running a playbook).
- **Never start, stop, restart, or resize VMs or containers (Incus, podman,
  Docker, systemd units that wrap them) without explicit user authorization
  for that specific action.** This includes `incus start/stop/restart/set`,
  `podman run/stop/restart`, and playbook tasks that trigger them. Changing
  resource limits (`limits.memory`, etc.) counts as resizing. Read-only
  inspection (`incus list`, `podman ps`, logs, config get) is always fine.
- **Don't create commits unless explicitly asked.** No commit prefixes; amend
  when fixing recently-committed code.
- **Never push to `main` without explicit authorization.** Work happens on a
  branch/worktree and is published as an MR; merging to `main`, direct pushes
  to `main`, and prod deploys of unmerged changes each require the user's
  explicit sign-off, even when the individual task ("deploy this") sounds
  like blanket approval for the whole chain.
- **Use `./tmp/`, never `/tmp/`, for scratch.** Never `rm -rf` paths that could
  contain user data — use `rmdir` or verify unmounted state first.
- **Don't swallow errors or stderr output.**

## Skills (auto-loaded)

- `testing` — unit tests (Python + Go) and dev-cluster system tests; pick the
  right surface for your change.
- `deploy-to-cluster` — dev-sync workflow, which playbook to run, `--limit managed`.
- `git-history-cleanup` — local history rewrites.

## Key docs

- **Running ansible** — `docs/operations/ansible.md` (agent session via
  cluster-exec on the bastion vs. directly on a core node; storage-leader
  detection; always resolve the leader before tailing logs; python package
  install rules).
- **Cluster exec tool** — `docs/operations/cluster-exec.md` (prod only; not
  the dev cluster; `hostname="localhost"` shortcut).
- **Architecture** — `docs/architecture.md` (node types, key services, network/IP
  ranges, repo layout, inventory).
- **etcd** — `docs/operations/etcd.md`. Backing store, not the interface: use
  the `ycluster` CLI and ansible, not `etcdctl`.
- **Authentik groups** — `docs/operations/authentik-groups.md` (app-role vs
  IdP-superuser groups; how to add members).
- **Inference gateway** — `docs/operations/inference.md` and
  `docs/local-ai-proxy.md`.
- **Dev PXE & dev cluster** — `docs/DEVELOPMENT.md`,
  `docs/design/virtual-test-environment.md` (Incus dev cluster).
- **Contributing** — `docs/CONTRIBUTING.md` (coding conventions, local
  conventions, third-party binaries policy).

The `ycluster` CLI runs on core nodes (`ycluster cluster status`,
`ycluster inference ls`, ...).
