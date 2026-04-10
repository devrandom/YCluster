# YCluster

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
docker compose exec ansible ansible-playbook admin/setup-admin-services.yml
```

Switch to normal mode:
```bash
docker compose down --profile bootstrap
docker compose up --build -d
```

### 3. Add More Nodes

PXE boot additional nodes (s2, s3, etc.). They will auto-provision based on MAC address.

### 4. Initialize Database

On s1:

```bash
ansible-playbook --tags setup-volumes,init-db site.yml storage/setup-storage-infrastructure.yml
```

and run the entire site playbook:

```bash
ansible-playbook site.yml
```

## Monitoring

- **Web Dashboard**: https://10.0.0.254/status (HTTPS with self-signed or Let's Encrypt certificates)
- **Command Line**: `check-cluster` on any node
- **Health API**: https://10.0.0.254/api/health
- **Certificate Status**: Automatic monitoring of certificate expiry and renewal

## Web Services

YCluster provides two main web interfaces:

- **Admin Interface**: Available at `https://admin.your-domain.com` - provides cluster management, monitoring, and administrative functions
- **Application Interface**: Available at `https://your-domain.com` - serves application services like Open-WebUI for AI chat

Both interfaces are automatically configured when you set up HTTPS certificates. The admin subdomain is always configured alongside your primary domain.  You just need to create a CNAME or ALIAS pointer from the admin subdomain to the main domain.

## Network Layout

- **Core nodes**: s1-s3 (10.0.0.11-13) - [etcd](docs/operations/etcd.md), admin services, [Ceph storage](docs/operations/ceph.md)
- **Storage nodes**: s4+ (10.0.0.14+) - additional Ceph storage capacity
- **Compute nodes**: c1+ (10.0.0.51+) - processing workloads
- **[macOS nodes](docs/operations/macos.md)**: m1+ (10.0.0.91+) - macOS compute nodes
- **AMT interfaces**: 10.10.10.x subnet (hostname + 'a' suffix)
- **WireGuard peers**: 10.0.1.x subnet - remote nodes joining via wg overlay
- **VIP**: 10.0.0.254 - cluster gateway and services

## Key Features

- **Self-bootstrapping** with MAC-based node detection
- **High availability** with etcd-based leader election and VIP failover
- **Distributed storage** using MicroCeph with RBD and XFS
- **Auto-discovery** and PXE provisioning
- **TLS certificates** with self-signed and Let's Encrypt integration
- **Reverse proxy** support via Rathole for external access
- **Comprehensive monitoring** with health checks, certificate tracking, and clock skew detection

## Certificate Management

Configure HTTPS domain and email for Let's Encrypt:

```bash
# Set primary domain and email
ycluster https set-domain your-domain.com
ycluster https set-email your-email@example.com

# Add additional domain aliases
ycluster https add-alias www.your-domain.com

# Obtain certificates (test mode first)
ycluster certbot obtain --test

# Production certificates
ycluster certbot obtain

# Check certificate status
ycluster certbot status
```

## External Access

Register an external server as a frontend node and deploy rathole server:

```bash
# Register the frontend server
ycluster frontend add f1 your-server.com --description "External rathole server"

# Deploy rathole server to the frontend node
ansible-playbook install-rathole-server.yml

# Configure cluster nodes to connect to the frontend server
ycluster rathole set --remote-addr "your-server.com:2333" --token "your_secret_token"
```

The rathole configuration provides two separate services:

- **Main rathole service**: Runs only on the storage leader and provides HTTP/HTTPS tunnels for web services
- **SSH rathole service**: Runs on all core nodes (s1, s2, s3) and provides individual SSH access tunnels

Each core node gets its own SSH tunnel endpoint on the rathole server. The SSH services bind to localhost on the rathole server, so access them by first SSH'ing into your frontend server, then connecting to the specific node:

Add this SSH config to your `~/.ssh/config` for easy access:

```
Host s?.rat
    HostName localhost
    User root
    ProxyJump your-frontend.net
Host s1.rat
    Port 2201
Host s2.rat
    Port 2202
Host s3.rat
    Port 2203
```

Then use direct access - `ssh s1.rat`.

## Remote Nodes (WireGuard)

Remote machines on the public internet can join the cluster via a WireGuard overlay. The cluster's public admin endpoint exposes a small whitelist (`/bootstrap/wg`, `/api/wg/register`, `/api/wg/poll/*`, plus the read-only status dashboard) — everything else under `/api` is cluster-subnet only.

On a fresh remote Linux box:

```bash
curl https://admin.your-domain.com/bootstrap/wg | sudo bash -s -- --type compute
```

On a remote macOS host (requires Homebrew installed for the invoking user):

```bash
curl https://admin.your-domain.com/bootstrap/wg-macos | sudo bash
```

Use `--dev` on an existing dev VM/mac to skip host-level mutations (hostname / admin user / SSH hardening) and drop the peer into the `dev` type (`d1..d30`, `10.0.1.201-230`). Both endpoints accept the same `--type` / `--dev` flags.

The script allocates a cluster hostname+IP (in `10.0.1.0/24` for wg peers), generates a keypair, registers with the admin API, and blocks on approval. On a core node:

```bash
ycluster wg list --pending
ycluster wg approve <hostname>
```

WG is orthogonal to node type — a wg-bootstrapped `compute` node is still a compute node with hostname `cN`, just with its IP in the wg peer subnet so the server's `wg0` can use a clean `/24` connected route. Peers reach cluster nodes through a static `10.0.1.0/24 via 10.0.0.254` route installed on every cluster host by `admin/setup-wg.yml`.

**Prerequisites:**
1. `ycluster wg init <public-endpoint>` on a core node (sets the externally-reachable wg endpoint, e.g. `vpn.your-domain.com:51820`)
2. The gateway VIP (`10.0.0.254`) reachable on UDP/51820 from the public side (typically a DNAT on your upstream router)
3. `/cluster/https/domain` set (used to render the `admin.<domain>` API URL in the bootstrap script)

## Inference Gateway

LiteLLM inference gateway provides a single OpenAI-compatible API at `http://inference.xc/v1/` (cluster-internal) and `https://your-domain.com/v1/` (external). Manage models with `ycluster inference add/remove`. Users share their Open-WebUI API keys for direct access.

**WARNING**: Do not add external LLM providers directly to Open-WebUI — user emails will leak in HTTP headers. Always add backends through LiteLLM. See [docs/operations/inference.md](docs/operations/inference.md) for details.

## Management Commands

All cluster management is now consolidated under the `ycluster` CLI:

- **Cluster Health**: `ycluster cluster status` - comprehensive cluster status
- **Node Management**: `ycluster dhcp` - manage DHCP allocations and leases
- **TLS Certificates**: `ycluster tls` - self-signed certificate management
- **HTTPS Configuration**: `ycluster https` - domain and certificate settings
- **Let's Encrypt**: `ycluster certbot` - SSL certificate operations
- **Frontend Nodes**: `ycluster frontend` - manage external access points
- **Rathole Configuration**: `ycluster rathole` - reverse proxy settings
- **WireGuard Overlay**: `ycluster wg` - remote-node onboarding and peer management
- **Storage Management**: `ycluster storage` - RBD volume operations
- **Inference Gateway**: `ycluster inference` - manage LiteLLM models and backends

### Getting Started with ycluster

Tab completions are available for bash.

```bash
# Show all available commands
ycluster --help

# Check cluster health
ycluster cluster status

# List DHCP allocations and leases
ycluster dhcp list all

# Generate self-signed certificates
ycluster tls generate --common-name your-domain.com
```

## SSH Access

Add to `~/.ssh/config`:
```
Host *.xc
  ProxyCommand sh -c 'ip=$(dig %h @10.0.0.254 +short); exec nc $ip 22'
  User root
  StrictHostKeyChecking no
```

## Development

### Setup

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Start the dev etcd container:

```bash
docker compose -f docker-compose.dev.yaml up -d etcd
```

### Running Ansible

Run ansible from the `dev/ansible/` directory:

```bash
cd dev/ansible
../../.venv/bin/ansible-inventory --list
../../.venv/bin/ansible-playbook ../../config/ansible/site.yml --check
```

The dev ansible.cfg uses `localhost:2379` for etcd inventory.

### Dev Services

Start the full dev stack (etcd + admin API):

```bash
docker compose -f docker-compose.dev.yaml up -d
```

- **etcd**: localhost:2379
- **Admin API**: localhost:12723
