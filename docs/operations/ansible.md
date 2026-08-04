# Running Ansible

## Run playbooks, never ad-hoc copies

Always use Ansible instead of ad-hoc copying files to the cluster. Keep
scripts in `config/ansible/scripts/`, installed by playbooks.

### From an agent session

Run prod playbooks via the cluster-exec MCP (`mcp__cluster_exec__run`) with
`hostname="localhost"` (the bastion, where `/etc/ansible` lives). See
`docs/operations/cluster-exec.md` for the full tool reference.

```text
run(command="cd /etc/ansible && ./run-playbook.sh admin/install-ycluster-package.yml",
    hostname="localhost")
```

cluster-exec hops through `$CE_BASTION` to the prod cluster only — it is
**not** a path to the dev cluster (`dev/cluster/`, local Incus) or to
`system-test.sh`; use `ssh -F dev/cluster/.ssh/config` or run Ansible from
`dev/cluster/` for those.

### From a core node directly

```bash
source /etc/ansible/env.sh  # sets LC_ALL=C.UTF-8 and ANSIBLE_VAULT_PASSWORD_FILE=/run/shm/.vault-pass
ansible-playbook site.yml
ansible-playbook storage/storage.yml --tags setup-volumes
```

## Storage leader detection

Tasks that must run only on the storage leader (e.g. PostgreSQL operations)
should detect it dynamically rather than
`delegate_to: "{{ groups['storage'][0] }}"`, which breaks with `--limit`:

```yaml
- name: Check if this host is the storage leader
  shell: mountpoint -q /rbd/user   # or /rbd/misc depending on the volume
  register: rbd_mounted
  failed_when: false
  changed_when: false

- name: Set storage leader fact
  set_fact:
    is_storage_leader: "{{ rbd_mounted.rc == 0 }}"

- name: Do something on storage leader only
  some_module:
    ...
  when: is_storage_leader
```

## Always resolve the storage leader before tailing logs

Leader-elected services (local-ai-proxy, PostgreSQL, Qdrant, DHCP) only run
on the storage leader — `journalctl -u <service>` on a non-leader shows
nothing. The leader can shift via ceph/mon election between sessions, so
always resolve it first:

```bash
cluster_exec.run(command="ycluster cluster leader", hostname="localhost")
cluster_exec.run(command="journalctl -u local-ai-proxy --since '10 minutes ago' --no-pager", hostname="LEADER")
```

## Python package installation

The `ycluster` package is installed via pip with `--break-system-packages --no-deps`.
All dependencies come from apt packages (e.g. `python3-requests`, `python3-flask`,
`python3-etcd3`, `python3-protobuf`). The install playbook removes any pip-installed
packages that shadow apt versions. Never install Python dependencies via pip — add
them to the apt list in `install-ycluster-package.yml`.

**Docker SDK errors**: "Not supported URL scheme http+docker" means
`requests >= 2.32` is installed via pip and shadowing the apt package. Check
`/usr/local/lib/python3.12/dist-packages/` for pip-installed packages that
shouldn't be there.

## Dev workflow (syncing local changes)

`dev-sync` uses rsync + an inotify daemon (Python) to continuously sync the
local repo to `$YC_SYNC_HOST:/opt/infrastructure/`; the user starts the
watcher. The `deploy-to-cluster` skill covers confirming a sync landed
(`./dev-sync --check`), which playbook to run for a given change, and the
`--limit managed` guard.
