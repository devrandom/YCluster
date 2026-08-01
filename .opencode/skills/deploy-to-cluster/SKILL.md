---
name: deploy-to-cluster
description: Deploy admin-area code changes to the cluster (let the watcher sync, run playbooks with --limit managed)
---

# Deploying to the cluster

## Cluster topology (relevant to deployment)

- The cluster groups nodes roughly as:
  - **`storage` (Ansible group)** — `s*` nodes. Run admin-api in full
    mode, plus everything else (postgres, registry, etc.). Currently
    the storage leader is the canonical public-facing admin-api node,
    but the leader can shift via ceph/mon election.
  - **`managed` (Ansible group)** — `storage` plus compute, NAS, and
    Nvidia nodes. **All `managed` nodes run admin-api**, including
    the non-storage ones — they serve only the status endpoint. This
    is heavy and should change (admin-api should ideally run only on
    storage), but is the current state. `macOS` and dev-cluster nodes
    are excluded from `managed`.
  - **`all`** — everything, including macOS/dev. Not used for
    admin-area deploys.
- Use Ansible groups as limits (`--limit storage`, `--limit managed`)
  rather than listing hostnames. They expand automatically.
- The storage leader can shift. `ycluster cluster leader` prints just
  the hostname — machine-readable, no awk on human output.

## Before deploying

Run tests on the **local dev cluster** first — see the `testing` skill for the right surface:

- Pure-function or single-component change → `just test` (Python + Go unit, ~30s).
- Cross-component change (etcd, leader election, nginx, ACME, anything spanning subsystems) → `just test-system` on the dev cluster.

A green run is the gate before the prod deploy below (the `--limit managed` step). A failed section means the deploy hides the bug rather than fixing it — fix the test first.

For the dev-cluster lifecycle (`dev-cluster.sh up`/`reset`/`purge`) and the `--assert-only` vs full-provisioning trade-off, see `testing`'s `references/system-test.md`.

## Deployment flow

1. **Commit locally** (no prefixes; match repo style).
2. **Files sync automatically** — `dev-sync`'s inotify daemon is
   started once per machine by direnv (outside agent sessions) and
   rsyncs the working tree to the cluster on every edit. The agent
   doesn't run `--once`; it just commits.
   - If unsure whether edits landed, run `dev-sync --check`
     — dry-run checksum compare, exits 0 on in-sync, prints differing
     files otherwise.
3. **SSH to any cluster node** (the operator-local entry alias is set
   by direnv; use whatever your shell resolves) and run the playbook:
   ```bash
   ssh <entry> 'cd /etc/ansible && \
     ./run-playbook.sh <playbook>.yml --limit managed'
   ```
   `./run-playbook.sh` sources `env.sh` and the vault password; don't
   call `ansible-playbook` directly. `--limit managed` is explicit so
   nothing accidentally targets `all`.

## Which playbook(s) for which change

| Change | Playbook | Limit | Notes |
|---|---|---|---|
| `config/ansible/admin/files/app.py` | `admin/setup-web-services.yml` | `managed` | Notifies `restart admin-api` handler when the file changes |
| `config/ansible/admin/files/templates/*.html` | `admin/setup-web-services.yml` | `managed` | Same — copies templates |
| `config/ansible/admin/files/ycluster/**/*.py` | `admin/install-ycluster-package.yml` | `managed` | Reinstalls the `ycluster` package via pip; restarts admin-api if the package changed |
| Both above (typical UI change) | Run both in sequence | `managed` | First `setup-web-services.yml`, then `install-ycluster-package.yml` |

Use `--limit managed` for all admin-area changes. The playbooks
target `managed` themselves; the explicit `--limit` is a guard
against accidentally targeting `all`.

Other subsystems (ceph, OWUI, monitoring, etc.) have their own
playbooks under `config/ansible/<area>/`. Pick the one whose
`files/` ships what you changed.

## Targeting only the leader (dev only)

For a leader-only dev run instead of `managed`:

```bash
ssh <entry> 'cd /etc/ansible && \
  LEADER=$(ycluster cluster leader) && \
  ./run-playbook.sh <playbook>.yml --limit "$LEADER"'
```

Other admin-api instances stay on old code during dev. This is fine
while testing since the public-facing app is the leader; promote to
`--limit managed` before committing the change.

## Verification

After the playbook(s) run, sanity-check on the leader:

```bash
LEADER=$(ssh <entry> 'ycluster cluster leader')
ssh <entry> "ssh $LEADER 'systemctl is-active admin-api'"
ssh <entry> "ssh $LEADER 'ls -l /opt/admin-services/app.py'"
```

`changed=N` in the playbook recap counts tasks that mutated state
(file copies, service restarts). `changed=0` means the cluster was
already up-to-date — if you expected a change, verify with
`dev-sync --check` that the sync actually landed.

## Gotchas

- **`dev-sync` overwrites files, not git state.** The cluster's
  `/opt/infrastructure/` working tree is rsync'd but its `.git/`
  stays at the last `git pull`. Don't expect `git log` on the cluster
  to match local.
- **The inotify daemon syncs to whatever `YC_SYNC_HOST` direnv
  set.** If you've overridden it for a flaky route, files may go
  elsewhere — `--check` shows what the watcher thinks is in sync.
- **Ansible handlers only fire on changed files.** If `restart
  admin-api` doesn't fire on a run you expected it to, the upstream
  copy task reported `ok` (no change) — investigate the playbook,
  don't work around it.
- **`ycluster cluster leader` reads etcd directly.** If etcd is
  down or no leader is elected, it exits non-zero with an error on
  stderr — that's a real cluster problem, not a parsing issue.
