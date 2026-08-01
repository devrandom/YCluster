# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

YCluster is a self-bootstrapping infrastructure platform for small AI clusters. It provisions bare metal servers via PXE boot, manages distributed storage with MicroCeph, and orchestrates services with etcd-based leader election.

## Critical Guidelines

- Do not leak production data into the repo.  For example:
  - emails
  - specific deployment URLs / hostnames / public IPs
  - user chosen VM names
  - any other user provided content / info
  - dates of any production event
  - hostnames with indexes > 3 (e.g. c4, s5)
- keep track of deployment specific tasks in NOTES.md (gitignored)
- **No prebuilt third-party binaries.** Cluster software comes from one of:
  - **apt** (Ubuntu repos)
  - **snap** (held, upgraded via playbooks — see `docs/operations/packages.md`)
  - **Build from source** in a dedicated `app/build-<name>.yml` or
    `admin/build-<name>.yml` playbook, source vendored under `/opt/src/`,
    pinned to a git SHA (e.g. `app/build-rathole.yml`, `app/build-open-webui.yml`)
  - **Docker images** built in-cluster, pushed to `registry.xc:5000`
  No `get_url`/`curl`/`wget` of compiled artifacts, no PyPI C-extension
  wheels, no `curl | bash`. If a third-party binary is genuinely needed,
  add a `build-<name>.yml` playbook — never `get_url` a binary into an
  install play.
- do not make changes to prod that affect security (e.g. firewall) or stability without checking with the user
- do not reboot any prod machine without explicit authorization

## Guidelines

- Keep this document concise.  Only use for orientation and information relevant to most sessions.  Keep
more specific knowledge in ./docs/ .
- Avoid doing things "manually" in prod if there is a best-practice way of doing it (e.g. running a playbook)

## Common Commands

### Running Ansible Playbooks

**From an agent session**, run prod playbooks via the cluster-exec MCP
(`mcp__cluster_exec__run`) with `hostname="localhost"` (the bastion,
where `/etc/ansible` lives). cluster-exec hops through `$CE_BASTION`
to the prod cluster only — it is **not** a path to the dev cluster
(`dev/cluster/`, local Incus) or to `system-test.sh`; use `ssh -F
dev/cluster/.ssh/config` or local ansible for those (see `## Dev Cluster
(local Incus)`). See `### Cluster exec via MCP` below for the full tool
reference.

Always use ansible instead of ad-hoc copying files to the cluster.

From a core node directly (operator already on a core node):
```bash
source /etc/ansible/env.sh  # sets LC_ALL=C.UTF-8 and ANSIBLE_VAULT_PASSWORD_FILE=/run/shm/.vault-pass
ansible-playbook site.yml
ansible-playbook storage/storage.yml --tags setup-volumes
```

### Dev Workflow (syncing local changes to cluster)
`dev-sync` uses rsync + an inotify daemon (Python) to continuously sync the local repo to `$YC_SYNC_HOST:/opt/infrastructure/`.
The user will take care of starting this process.

**Confirming a sync landed (don't hash files by hand).** Trust the watcher by
default; for a routine confirmation, `tail ./tmp/dev-sync.log` for a `done` line
stamped after your edit (the trigger only logs `done` on rsync exit 0, so
partial syncs show as `rsync failed`). When you want certainty (before a
high-stakes playbook run, or you suspect drift), run `./dev-sync --check`
— a dry-run checksum compare against the cluster using the same exclude set; it
also reports whether the watcher is running. Empty diff + exit 0 means in sync,
exit 1 lists the files that differ. Never per-file
`cmp`/`sha256` over SSH (noisy), and don't rely on a git-hash check — the synced
set is defined by `.watchignore` and includes untracked scratch dirs that a
tracked-files-only hash can't see.

### Cluster Management
The `ycluster` CLI is installed on core nodes:

```bash
ycluster cluster status
ycluster inference ls
...
```

### Held Packages
Some packages are deliberately held from auto-upgrade (apt: `postgresql-16`;
snap: `microceph`). Upgrade them only via their playbooks — never ad-hoc
apt/snap commands (the playbooks handle serialization, leader ordering, and
the staleness-monitoring timestamp):
```bash
./run-playbook.sh storage/upgrade-held-packages.yml   # apt holds
./run-playbook.sh storage/upgrade-microceph.yml       # microceph snap
```
See `docs/operations/packages.md`.

## Architecture

### Node Types
- **Core nodes (s1-s3)**: etcd cluster, admin services (DHCP, DNS, PXE), MicroCeph storage
- **Storage nodes (s4+)**: Additional Ceph storage, run stateful services via leader election
- **Compute nodes (c1+)**: Processing workloads
- **macOS nodes (m1+)**: macOS compute nodes, bootstrapped via `/bootstrap/macos` endpoint
- **Nvidia nodes (nv1+)**: Ubuntu-based Nvidia GPU servers, bootstrapped via `/bootstrap/nvidia` endpoint
- **NAS nodes (nas1+)**: Ubuntu-based NAS devices, bootstrapped via `/bootstrap/nas` endpoint (dynamic IP)
- **Frontend nodes (f1+)**: External access via Rathole reverse proxy. When registered by IP they also get an internal `.xc` DNS record, so they're reachable from inside the cluster by name (`ssh f1.xc`, `ping f1`)
- **Adhoc nodes (x1-x49)**: Ad-hoc nodes that join by setting hostname before DHCP (no Ansible required)

### Key Services
- **etcd**: Cluster state and configuration (single source of truth)
- **MicroCeph**: Distributed block storage with RBD
- **Leader election**: etcd-based single-instance coordination for PostgreSQL, Qdrant, DHCP
- **Keepalived**: VIP failover for gateway (10.0.0.254) and storage (10.0.0.100)
- **local-ai-proxy**: Inference gateway at `inference.xc` (port 4001, nginx-fronted with auth_request), routing to vLLM/llama-server/whisperx backends

### Network
- **Gateway VIP (10.0.0.254)**: Routing, DHCP, DNS - tied to storage nodes (control plane)
- **Storage VIP (10.0.0.100)**: Admin API, Docker registry - tied to storage nodes (needs etcd)
- **DNS suffix**: `.xc` for cluster hostnames (e.g., `admin.xc`, `registry.xc`, `inference.xc`, `s1.xc`, `nv1.xc`)
- **IP ranges by node type**:
  - Storage (s1-s20): 10.0.0.11-30
  - Compute (c1-c20): 10.0.0.51-70
  - macOS (m1-m20): 10.0.0.91-110
  - Nvidia (nv1-nv20): 10.0.0.111-130
  - NAS (nas1-nas10): 10.0.0.131-140
  - Adhoc (x1-x49): 10.0.0.151-199
  - Dynamic (dhcp-NNN): 10.0.0.200-249
- **.xc**: the cluster-internal domain
- The local machine is not inside the cluster, so cannot resolve this.

### Directory Structure
- `config/ansible/` - All Ansible playbooks and configuration
  - `site.yml` - Main orchestration playbook
  - `admin/` - Admin services (DHCP, PXE, web services, ycluster package)
  - `storage/` - Storage infrastructure (Ceph, PostgreSQL, Qdrant)
  - `app/` - Application deployments (Open-WebUI, Rathole)
  - `monitoring/` - Prometheus, Grafana, alerting
  - `inventory_plugins/etcd_nodes.py` - Dynamic inventory from etcd
- `config/ansible/admin/files/ycluster/` - Python CLI tool source
  - `app/` - Application deployments (Open-WebUI, local-ai-proxy, Rathole)
- `docs/` - documentation

### Inventory
Ansible inventory is auto-loaded from `inventory_boot.yml` and `inventory_etcd.yml`. The `etcd_nodes` inventory plugin dynamically discovers nodes.

## Coding Conventions

- No commit message prefixes (no "feat:", "fix:", etc.)
- Be terse. Short commit subjects; no rationale essays in bodies.
  Rationale that needs to survive goes in `docs/`.
- Do not create commits unless explicitly asked by the user
- Amend when fixing code that was recently committed
- Run the relevant tests/build before committing. If the local toolchain
  doesn't have what's needed, stop and tell the user — never commit untested code.
- Keep scripts in `config/ansible/scripts/` directory, installed by playbooks
- Prefer Python libraries over subprocess for mainstream tasks
- Playbooks must be idempotent and minimize suprious change detection
- Do not swallow errors or stderr output
- Use `git mv` for file renames
- **Use `./tmp/` not `/tmp/`** for local scratch files (logs, system-test
  output, temp configs). `./tmp/` is gitignored. The system-test logs at
  `./tmp/system-test-<section>.log` follow this convention.
- **Pipe long command output locally through `headtail`** (on PATH at
  `~/bin/headtail`) instead of `head`/`tail` so the start and end of the
  output are both visible without truncating either. Use `headtail -l FILE`
  to also tee the full un-elided output to a log file.
- Do not use `tail` or `headtail` inside the cluster-exec tool.  It already trims long output.
- Use JSON format for command output when available (e.g., `rbd --format json`)
- Add JSON output format to custom scripts used by other scripts
- **NEVER run `rm -rf` on mount points or paths that could contain user data without confirming with the user first** - use `rmdir` for empty directories or verify unmounted state first
- `research/`, `bench-results/`, `go-bench`, and `env.sh` are local files — do not commit them

## Ansible Patterns

### Storage Leader Detection
Tasks that need to run only on the storage leader (e.g., PostgreSQL operations) should detect it dynamically rather than using `delegate_to: "{{ groups['storage'][0] }}"` which breaks with `--limit`:

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

### Python Package Installation
The `ycluster` package is installed via pip with `--break-system-packages --no-deps`. All dependencies come from apt packages (e.g., `python3-requests`, `python3-flask`, `python3-etcd3`, `python3-protobuf`). The install playbook removes any pip-installed packages that shadow apt versions. Never install Python dependencies via pip - add them to the apt list in `install-ycluster-package.yml`.

## Troubleshooting

### MicroCeph Issues
If `ceph -s` works but `snap logs microceph` shows dqlite errors, check the `snap.microceph.daemon` service on all nodes. The dqlite cluster needs 2/3 nodes for quorum. Restart failed daemons with:
```bash
sudo systemctl restart snap.microceph.daemon.service
```

### Always check the storage leader before tailing logs
Leader-elected services (`local-ai-proxy`, PostgreSQL, Qdrant, DHCP) only
run on the storage leader — `journalctl -u <service>` on a non-leader shows
nothing. The leader can shift via ceph/mon election between sessions, so
always resolve it first:
```bash
cluster_exec.run(command="ycluster cluster leader", hostname="localhost")
cluster_exec.run(command="journalctl -u local-ai-proxy --since '10 minutes ago' --no-pager", hostname="LEADER")
```

### Docker SDK Errors
"Not supported URL scheme http+docker" means `requests >= 2.32` is installed via pip and shadowing the apt package. Check `/usr/local/lib/python3.12/dist-packages/` for pip-installed packages that shouldn't be there.

## Etcd (backing store — not the interface)

etcd is the source of truth, but it is **not** how you interact with the
cluster: change state through the **`ycluster` CLI** and **ansible**, which own
validation, hot-reload, and leader/serialization semantics. Reaching for
`etcdctl` (especially to write) is a smell. These keys are listed for
*understanding and read-only debugging* — each write goes through the tool that
owns it:

- `/cluster/nodes/by-hostname/<name>` — node registration (written by DHCP/bootstrap; read via `ycluster cluster status`)
- `/cluster/config/inference/master-key` — cluster admin bearer (validated by local-ai-proxy-auth.service)
- `/cluster/config/inference/models/<name>` — model→backend map, JSON `{"backends":[{"api_base":"..."}, ...]}`, hot-reloaded — **manage with `ycluster inference add/remove`** (see `docs/operations/inference.md`)
- `/cluster/config/inference/disabled/<url>` — backends out of rotation — `ycluster inference disable/enable`
- `/cluster/services/` — service state & leader election (written by the services themselves; read via `ycluster cluster status`)

## Inference Gateway (local-ai-proxy)

`local-ai-proxy.service` runs on the **storage leader only** (part of `ycluster-apps.target`, so it follows the leader/VIP — not on every storage node), listening on `127.0.0.1:4001`. nginx fronts it at `http://inference.xc/v1/` (cluster-internal; `inference.xc` → storage VIP `10.0.0.100`) and `https://<domain>/v1/` (external) with `auth_request` enforcing per-user bearer auth via `local-ai-proxy-auth.service` on `127.0.0.1:4002` (which reads Open-WebUI's `api_key` table from the leader-local postgres). Non-leaders correctly run neither. Accepted tokens: the cluster admin master key in etcd, or a row in Open-WebUI's `api_key` table.

For usage and operations (adding models, API keys, etc.), see `docs/operations/inference.md`.

## Dev PXE Environment

`dev/` has a local PXE boot setup for testing autoinstall. Run `just dev-tftpboot` then `sudo just dev-dnsmasq-watch`. See `dev/` files for details.

## Dev Cluster (local Incus)

`dev/cluster/` stands up a throwaway local ycluster on Incus system containers
(s1-s3 core, c1, f1) for testing infra changes without touching prod — see
`docs/design/virtual-test-environment.md`.

This surface is **local only** — cluster-exec (prod-only, hops via
`$CE_BASTION`) cannot reach these containers. Use `ssh -F
dev/cluster/.ssh/config`, `incus exec`, or run ansible from
`dev/cluster/` directly (`../../venv/bin/ansible-playbook site-dev.yml`).
`just test-system` and `just test-system-fresh` run the end-to-end
assertions here; logs land in `./tmp/system-test-<section>.log`.

```bash
dev/cluster/dev-cluster.sh status            # is it up? (up/down/reset/exec are operator-run)
ssh -F dev/cluster/.ssh/config s1 <cmd>      # ssh into a dev node (key/config generated by `up`)
```

## Third Party Code

If you need to investigate third-party code, clone it in ./ext/ .

## Codex CLI Specific Guidance

### `exec_command`

When `exec_command` returns a `session_id` (or the response body says
"Process running with session ID N"), the shell is still running in the
background. Reuse it via `write_stdin(session_id=N)` to read more output
or send input, or terminate it via `thread/backgroundTerminals/terminate`.
Do not re-run the same command — that leaks the background process.

An empty `output` field does not mean the command finished. Check
`session_id` and `exit_code` (or the "Process running"/"Process exited"
line) before deciding.

Background terminals persist across turns. If you started one earlier in
the session, you can find it in the active-background-terminals fragment
injected into your context.

### Cluster exec via MCP

`mcp/cluster-exec` is a local MCP server exposing four tools: `run` (execute
a shell command on an internal host via the two-hop SSH pattern above),
`tail` (render the tail of the JSONL log as a human-readable transcript),
`status` (list or inspect backgrounded runs), and `kill` (terminate a
backgrounded run by session_id). It runs as a streamable-HTTP daemon on
`http://127.0.0.1:4003/mcp` and is registered in `opencode.json` as a
`remote` MCP.  The user will handle starting it.

**Prod only.** `run` hops through `$CE_BASTION` to prod hosts — the
dev cluster (`dev/cluster/`, local Incus containers) and
`system-test.sh` runs are not reachable through it. For those surfaces
use `ssh -F dev/cluster/.ssh/config` or run ansible from `dev/cluster/`
directly.

Full stdout/stderr is logged (untruncated) to `./tmp/cluster.log` as JSONL
events; the LLM-facing tool result returns only the head/tail (defaults
25/25) of the chronologically merged stream with `[stderr]` tags. Progress
notifications tick every second while a long command runs. **Foreground
is capped at 10s by default** (`CE_AUTO_BG_TIMEOUT_S`) — the `run` tool
call always returns within 10s, either with the result or with
`BACKGROUNDED session=<id>`. Bump the env var (e.g.
`CE_AUTO_BG_TIMEOUT_S=120 bash mcp/cluster-exec/cedaemon --start`) for
deployments where a known-long command should hold the foreground
rather than auto-backgrounding; the default workflow is to poll with
`status` + `tail -s <id>` / `kill` after it backgrounds. The hard
backstop on the backgrounded process itself is `timeout_s` (default
1800s, `CE_TIMEOUT_S`; SIGTERM, then SIGKILL after 2s). If the HTTP
client disconnects mid-`run` (e.g. the turn is interrupted), the SSH
proc is also registered as a backgrounded session — the response is
gone, so find it with `status` (no args) or `tail -s <id>` and recover
with `kill`.

`status` and `kill` only see the caller's own backgrounded runs (filtered
by the caller's `_meta.threadId`, falling back to the `mcp-session-id`
header); other clients' sessions are not enumerable. The bearer token
alone authenticates the connection, but per-client visibility is scoped
by the caller's stable identifier — one client cannot `status` or `kill`
another's runs. The JSONL log remains the source of truth for everything
(read-only via `tail -s <id>`).

`_meta.threadId` is the conventional MCP slot for a stable per-process
session identifier — codex puts its thread ID there on every `tools/call`,
so the principal survives a transport-level reconnect within the same
codex invocation. Clients that don't populate `_meta.threadId` (e.g.
opencode) fall back to the `mcp-session-id` header, which is
**per-connection** — the streamable-HTTP client reuses the same ID
across reconnects within one process, but the server's session manager
can expire it independently, and a fresh process invocation (opencode
restart, codex resume) gets a new ID and cannot see prior orphans.
Either way, orphans are still readable via `tail -s <id>` and get
cleaned up by the hard `timeout_s` watcher or by `cedaemon --stop`.

**`hostname="localhost"` shortcut**: use it for anything
that should run on the bastion itself, e.g. ansible playbooks:
```text
run(command="cd /etc/ansible && ./run-playbook.sh admin/install-ycluster-package.yml",
    hostname="localhost")
```

Avoid the use of ssh in the command - specify the hostname as a tool call argument instead.

**`sudo -u <user>` quirk on nvidia hosts**: cluster-exec lands as root, so
`sudo -u dev podman …` would normally work, but sudo's `env_reset` tries
to chdir to the invoker's home (`/root`), which fails with
`cannot chdir to /root: Permission denied`. Workaround: prefix the
command with `cd /` so sudo has nowhere to chdir. Example:
```text
run(command="cd / && sudo -u dev podman ps --filter name=ds4",
    hostname="nv4")
```
Applies to any prod host where the target user (e.g. `dev`) doesn't have
read access to the invoker's home.

