# XCluster

Self-bootstrapping infrastructure platform for small AI clusters using Ceph, PostgreSQL, and Qdrant.

## Quick Start

### 1. Bootstrap First Node (s1)

On admin laptop, start PXE environment:
```bash
docker compose up --build --profile bootstrap -d
```

PXE boot s1 and wait for OS installation to complete.

### 2. Deploy Admin Services

From admin laptop:
```bash
docker compose exec ansible ansible-playbook setup-admin-services.yml
```

Switch to normal mode:
```bash
docker compose down --profile bootstrap
docker compose up --build -d
```

### 3. Add More Nodes

PXE boot additional nodes (s2, s3, etc.). They will auto-provision based on MAC address.

### 4. Initialize Database

```bash
ansible-playbook --tags init_db install-postgres.yml install-storage-leader-election.yml
```

## Monitoring

- **Web Dashboard**: http://10.0.0.254/status
- **Command Line**: `check-cluster` on any node
- **Health API**: http://10.0.0.254/api/health

## Network Layout

- **Core nodes**: s1-s3 (10.0.0.11-13) - etcd, admin services, storage
- **Storage nodes**: s4+ (10.0.0.14+) - additional storage capacity  
- **Compute nodes**: c1+ (10.0.0.51+) - processing workloads
- **AMT interfaces**: 10.10.10.x subnet (hostname + 'a' suffix)
- **VIP**: 10.0.0.254 - cluster gateway and services

## Key Features

- **Self-bootstrapping** with MAC-based node detection
- **High availability** with etcd-based leader election
- **Distributed storage** using MicroCeph with RBD
- **Auto-discovery** and PXE provisioning
- **TLS certificates** with Let's Encrypt integration
- **Reverse proxy** support via Rathole

## External Access

Register an external server as a frontend node and deploy rathole server:

```bash
# Register the frontend server
frontend-manager add f1 your-server.com --description "External rathole server"

# Deploy rathole server to the frontend node
ansible-playbook install-rathole-server.yml --limit frontend

# Configure cluster nodes to connect to the frontend server
rathole-config set --remote-addr "your-server.com:2333" --token "your_secret_token"
```

## SSH Access

Add to `~/.ssh/config`:
```
Host *.xc
  ProxyCommand sh -c 'ip=$(dig %h @10.0.0.254 +short); exec nc $ip 22'
  User root
  StrictHostKeyChecking no
```

