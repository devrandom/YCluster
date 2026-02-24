# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

YCluster is a self-bootstrapping infrastructure platform for small AI clusters. It provisions bare metal servers via PXE boot, manages distributed storage with MicroCeph, and orchestrates services with etcd-based leader election.

## Common Commands

### Bootstrap (from admin laptop)
```bash
docker compose up --build --profile bootstrap -d
docker compose exec ansible ansible-playbook admin/setup-admin-services.yml
```

### Running Ansible Playbooks
From a core node (s1-s3) via SSH — preferred for development:
```bash
# /etc/ansible is a symlink to /opt/infrastructure/config/ansible
# run-playbook.sh sources /etc/ansible/env.sh (sets LC_ALL and ANSIBLE_VAULT_PASSWORD_FILE)
ssh s2.yc "cd /etc/ansible && ./run-playbook.sh <playbook>.yml"
ssh s2.yc "cd /etc/ansible && ./run-playbook.sh <playbook>.yml --limit s2"
```

From a core node directly:
```bash
source /etc/ansible/env.sh  # sets LC_ALL=C.UTF-8 and ANSIBLE_VAULT_PASSWORD_FILE=/run/shm/.vault-pass
ansible-playbook site.yml
ansible-playbook storage/storage.yml --tags setup-volumes
```

### Dev Workflow (syncing local changes to cluster)
`dev-sync.sh` uses rsync + watchman to continuously sync the local repo to `s2.yc:/opt/infrastructure/`:
```bash
bash dev-sync.sh  # runs initial sync then watches for changes
```
After making changes, either wait for watchman to sync or run manually:
```bash
rsync -rvl --exclude-from .syncignore ./ s2.yc:/opt/infrastructure/
```
Then run the playbook via SSH as above.

### Cluster Management
The `ycluster` CLI is installed on core nodes:
```bash
ycluster cluster status
ycluster dhcp list all
ycluster tls generate --common-name your-domain.com
ycluster certbot obtain --test
```

## Architecture

### Node Types
- **Core nodes (s1-s3)**: etcd cluster, admin services (DHCP, DNS, PXE), MicroCeph storage
- **Storage nodes (s4+)**: Additional Ceph storage, run stateful services via leader election
- **Compute nodes (c1+)**: Processing workloads
- **macOS nodes (m1+)**: macOS compute nodes, bootstrapped via `/bootstrap/macos` endpoint
- **Nvidia nodes (nv1+)**: Ubuntu-based Nvidia GPU servers, bootstrapped via `/bootstrap/nvidia` endpoint
- **NAS nodes (nas1+)**: Ubuntu-based NAS devices, bootstrapped via `/bootstrap/nas` endpoint (dynamic IP)
- **Frontend nodes (f1+)**: External access via Rathole reverse proxy
- **Adhoc nodes (x1-x49)**: Ad-hoc nodes that join by setting hostname before DHCP (no Ansible required)

### Key Services
- **etcd**: Cluster state and configuration (single source of truth)
- **MicroCeph**: Distributed block storage with RBD
- **Leader election**: etcd-based single-instance coordination for PostgreSQL, Qdrant, DHCP
- **Keepalived**: VIP failover for gateway (10.0.0.254) and storage (10.0.0.100)

### Network
- **Gateway VIP (10.0.0.254)**: Routing, DHCP, DNS - may move to non-storage nodes
- **Storage VIP (10.0.0.100)**: Admin API, Docker registry - tied to storage nodes (needs etcd)
- **DNS suffix**: `.xc` for cluster hostnames (e.g., `admin.xc`, `registry.xc`, `s1.xc`)
- **IP ranges by node type**:
  - Storage (s1-s20): 10.0.0.11-30
  - Compute (c1-c20): 10.0.0.51-70
  - macOS (m1-m20): 10.0.0.91-110
  - Nvidia (nv1-nv20): 10.0.0.111-130
  - NAS (nas1-nas10): 10.0.0.131-140
  - Adhoc (x1-x49): 10.0.0.151-199
  - Dynamic (dhcp-NNN): 10.0.0.200-249

### Directory Structure
- `config/ansible/` - All Ansible playbooks and configuration
  - `site.yml` - Main orchestration playbook
  - `admin/` - Admin services (DHCP, PXE, web services, ycluster package)
  - `storage/` - Storage infrastructure (Ceph, PostgreSQL, Qdrant)
  - `app/` - Application deployments (Open-WebUI, Rathole)
  - `monitoring/` - Prometheus, Grafana, alerting
  - `inventory_plugins/etcd_nodes.py` - Dynamic inventory from etcd
- `config/ansible/admin/files/ycluster/` - Python CLI tool source

### Inventory
Ansible inventory is auto-loaded from `inventory_boot.yml` and `inventory_etcd.yml`. The etcd_nodes inventory plugin dynamically discovers nodes.

## Coding Conventions

- No commit message prefixes (no "feat:", "fix:", etc.)
- Keep scripts in `config/ansible/scripts/` directory, installed by playbooks
- Prefer Python libraries over subprocess for mainstream tasks
- Playbooks must be idempotent
- Do not swallow errors or stderr output
- Use `git mv` for file renames
- Use JSON format for command output when available (e.g., `rbd --format json`)
- Add JSON output format to custom scripts used by other scripts
- **NEVER run `rm -rf` on mount points or paths that could contain user data without confirming with the user first** - use `rmdir` for empty directories or verify unmounted state first

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

### Docker SDK Errors
"Not supported URL scheme http+docker" means `requests >= 2.32` is installed via pip and shadowing the apt package. Check `/usr/local/lib/python3.12/dist-packages/` for pip-installed packages that shouldn't be there.

## Etcd Paths
- `/cluster/nodes/by-hostname/<name>` - Node registration
- `/cluster/config/` - Cluster configuration
- `/cluster/services/` - Service state and leader election

## Dev PXE Environment

`dev/` has a local PXE boot setup for testing autoinstall. Run `make dev-tftpboot` then `sudo make dev-dnsmasq-watch`. See `dev/` files for details.