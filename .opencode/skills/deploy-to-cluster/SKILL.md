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
2. **Files sync automatically — `dev-sync` is user-managed.** Its
   inotify daemon is started once per machine by the user (via direnv,
   outside agent sessions) and rsyncs the working tree to the cluster
   on every edit. The agent doesn't run it and may not even have
   `dev-sync` on its PATH; the agent just commits and lets the user's
   watcher sync.
   - If unsure whether edits landed, don't run `dev-sync --check`
     yourself — confirm the file is present on the bastion through
     cluster-exec instead (e.g. `grep`/`test -f` against
     `/etc/ansible/...`), or ask the user to confirm the sync.

   **Path mapping (easy to get wrong):** on the bastion `/etc/ansible`
   is a symlink to `/opt/infrastructure/config/ansible`. So the local
   repo's `config/ansible/` tree maps **directly** onto `/etc/ansible/`
   on the bastion — local `config/ansible/admin/templates/foo.j2` is
   `/etc/ansible/admin/templates/foo.j2`. Do **not** write
   `/etc/ansible/config/ansible/...` (doubling the prefix: that path
   does not exist). `/etc/ansible/` is itself where `run-playbook.sh`
   and `env.sh` live (via the symlink).

3. **Run the playbook via cluster-exec** (`run` tool; see
   `docs/operations/cluster-exec.md` for the full tool reference).
   Use `hostname="localhost"` for anything that should run on the
   bastion, where `/etc/ansible` lives:
   ```
   run(command="cd /etc/ansible && ./run-playbook.sh <playbook>.yml --limit managed",
       hostname="localhost")
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

For a leader-only dev run instead of `managed`, resolve the leader and
pass it to the play `--limit` in a single cluster-exec run on the
bastion (`$(ycluster cluster leader)` is evaluated there):

```
run(command="cd /etc/ansible && ./run-playbook.sh <playbook>.yml --limit $(ycluster cluster leader)",
    hostname="localhost")
```

Other admin-api instances stay on old code during dev. This is fine
while testing since the public-facing app is the leader; promote to
`--limit managed` before committing the change.

## Verification

After the playbook(s) run, resolve the leader with cluster-exec, then
sanity-check it by passing the leader as the `hostname` argument — the
cluster-exec hop target — rather than nesting a shell hop inside the
command:

```
run(command="ycluster cluster leader", hostname="localhost")  # prints the leader hostname
run(command="systemctl is-active admin-api", hostname="<leader>")
run(command="ls -l /opt/admin-services/app.py", hostname="<leader>")
```

`changed=N` in the playbook recap counts tasks that mutated state
(file copies, service restarts). `changed=0` means the cluster was
already up-to-date — if you expected a change, confirm the synced
file landed on the bastion via cluster-exec (e.g. `test -f`/`grep`
in `/etc/ansible/...`) rather than running `dev-sync --check`
yourself.

## Gotchas

- **`dev-sync` overwrites files, not git state.** The cluster's
  `/opt/infrastructure/` working tree is rsync'd but its `.git/`
  stays at the last `git pull`. Don't expect `git log` on the cluster
  to match local.
- **The inotify daemon syncs to whatever `YC_SYNC_HOST` direnv
  set.** If it's been overridden for a flaky route, files may go
  elsewhere. `dev-sync` is user-managed.
- **Ansible handlers only fire on changed files.** If `restart
  admin-api` doesn't fire on a run you expected it to, the upstream
  copy task reported `ok` (no change) — investigate the playbook,
  don't work around it.
- **`ycluster cluster leader` reads etcd directly.** If etcd is
  down or no leader is elected, it exits non-zero with an error on
  stderr — that's a real cluster problem, not a parsing issue.
