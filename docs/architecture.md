# Architecture

## Node Types

- **Core nodes (s1-s3)**: etcd cluster, admin services (DHCP, DNS, PXE), MicroCeph storage
- **Storage nodes (s4+)**: Additional Ceph storage, run stateful services via leader election
- **Compute nodes (c1+)**: Processing workloads
- **macOS nodes (m1+)**: macOS compute nodes, bootstrapped via `/bootstrap/macos` endpoint
- **Nvidia nodes (nv1+)**: Ubuntu-based Nvidia GPU servers, bootstrapped via `/bootstrap/nvidia` endpoint
- **NAS nodes (nas1+)**: Ubuntu-based NAS devices, bootstrapped via `/bootstrap/nas` endpoint (dynamic IP)
- **Frontend nodes (f1+)**: External access via frp reverse proxy — public HTTP/HTTPS tunnels to the storage leader's `127.0.0.2`. When registered by IP they also get an internal `.xc` DNS record, so they're reachable from inside the cluster by name (`ssh f1.xc`, `ping f1`). Full path + how to add a public vhost: `docs/operations/public-routing.md`.
- **Adhoc nodes (x1-x49)**: Ad-hoc nodes that join by setting hostname before DHCP (no Ansible required)

## Key Services

- **etcd**: Cluster state and configuration (single source of truth)
- **MicroCeph**: Distributed block storage with RBD
- **Leader election**: etcd-based single-instance coordination for PostgreSQL, Qdrant, DHCP
- **Keepalived**: VIP failover for gateway (10.0.0.254) and storage (10.0.0.100)
- **local-ai-proxy**: Inference gateway at `inference.xc` (nginx-fronted with auth_request), routing to vLLM/llama-server/whisperx backends

## Network

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
- **.xc**: the cluster-internal domain. The local machine is not inside the cluster, so it cannot resolve these names.

## Directory Structure

- `config/ansible/` - All Ansible playbooks and configuration
  - `site.yml` - Main orchestration playbook
  - `admin/` - Admin services (DHCP, PXE, web services, ycluster package)
  - `storage/` - Storage infrastructure (Ceph, PostgreSQL, Qdrant)
  - `app/` - Application deployments (Open-WebUI, frp)
  - `monitoring/` - Prometheus, Grafana, alerting
  - `inventory_plugins/etcd_nodes.py` - Dynamic inventory from etcd
- `config/ansible/admin/files/ycluster/` - Python CLI tool source
- `docs/` - documentation
- `dev/` - local PXE harness and `dev/cluster/` (Incus dev cluster)

## Inventory

Ansible inventory is auto-loaded from `inventory_boot.yml` and `inventory_etcd.yml`. The `etcd_nodes` inventory plugin dynamically discovers nodes.

## Cluster management

The `ycluster` CLI is installed on core nodes (`ycluster cluster status`, `ycluster inference ls`, ...). etcd is the backing store but not the interface — see `docs/operations/etcd.md`.
