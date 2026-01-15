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

- **Core nodes**: s1-s3 (10.0.0.11-13) - etcd, admin services, storage
- **Storage nodes**: s4+ (10.0.0.14+) - additional storage capacity  
- **Compute nodes**: c1+ (10.0.0.51+) - processing workloads
- **AMT interfaces**: 10.10.10.x subnet (hostname + 'a' suffix)
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

## Management Commands

All cluster management is now consolidated under the `ycluster` CLI:

- **Cluster Health**: `ycluster cluster status` - comprehensive cluster status
- **Node Management**: `ycluster dhcp` - manage DHCP allocations and leases
- **TLS Certificates**: `ycluster tls` - self-signed certificate management
- **HTTPS Configuration**: `ycluster https` - domain and certificate settings
- **Let's Encrypt**: `ycluster certbot` - SSL certificate operations
- **Frontend Nodes**: `ycluster frontend` - manage external access points
- **Rathole Configuration**: `ycluster rathole` - reverse proxy settings
- **Storage Management**: `ycluster storage` - RBD volume operations

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
