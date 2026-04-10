# YCluster Architecture

## Overview

YCluster is a self-bootstrapping infrastructure platform that creates highly available clusters from bare metal servers. It provides automated provisioning, distributed storage, and service orchestration with built-in failover.

## Key Design Principles

1. **Self-Bootstrapping**: Minimal manual intervention required
2. **High Availability**: No single points of failure
3. **Auto-Discovery**: MAC-based node type detection
4. **Declarative Configuration**: Ansible-driven infrastructure
5. **Distributed State**: etcd as single source of truth

This architecture enables resilient, self-managing infrastructure that scales from small deployments to larger clusters while maintaining operational simplicity.

## Key Features

- **Self-Healing**: Automatic leader election, Ceph rebalancing, service auto-restart
- **Monitoring**: Health APIs, web dashboard, leadership tracking
- **Security**: SSH keys, isolated network, controlled proxy access
- **Zero-Downtime**: Rolling updates, graceful migration, cluster expansion

## System Architecture

### Node Types

**Core Nodes (s1, s2, s3)**
- etcd cluster and admin services (DHCP, DNS, PXE boot, cluster APIs)
- Keepalived VIP and MicroCeph storage

**Storage Nodes (s4+)**
- MicroCeph with RBD volumes and exclusive locking
- Stateful services (PostgreSQL, Qdrant, Docker registry) with leader election
- Storage leader manages Rathole reverse proxy
- Encrypted user RBD volumes with Tang-based key management

**macOS Nodes (m\*)**
- Client nodes providing additional compute capacity
- Health monitoring and network connectivity checks

**Compute Nodes (c\*)**
- Provide processing capacity for workloads

**Frontend Nodes (f\*)**
- Provide external access via Rathole reverse proxy
- Registered in etcd for dynamic configuration

### Core Services

**Core Services**
- **etcd**: Cluster state and configuration
- **MicroCeph**: Distributed block storage with RBD
- **Leader Election**: Single active instance for stateful services
- **Keepalived**: VIP failover for admin and storage services
- **Docker Registry**: Container images with RBD backend

**Application Services**
- **Open-WebUI**: AI chat interface behind nginx reverse proxy (slim build)
- **Tang**: Network-bound encryption key server for RBD volume encryption
- **Secrets Volume**: Encrypted storage for sensitive cluster data

### Security & Secrets Management
- **TLS Infrastructure**: Self-signed certificates and Let's Encrypt integration
- **Certificate Management**: Synchronization via etcd and nginx with automated renewal
- **Secrets Volume**: Encrypted RBD volume for sensitive data storage
- **Tang Server**: Network-bound encryption for automatic RBD volume unlocking
- **Clevis Integration**: Automated decryption of encrypted volumes on boot

### Storage Management
- **Backup System**: Automated database backups to encrypted storage
- **Volume Encryption**: At-rest encryption for all user data volumes

### Web Services Architecture
- Application-specific reverse proxy configurations

### High Availability

- **Leader Election**: etcd-based failover for PostgreSQL, Qdrant, DHCP
- **Storage**: Ceph replication, RBD exclusive locking, automatic rebalancing
- **Network**: VIP failover, redundant DNS/DHCP/proxy services
- **Uplink Service VIP**: Shared external IP that follows storage leader for incoming connections
- **Monitoring**: Health checks, cluster status monitoring, service health APIs
- **Backup & Recovery**: Automated database backups with encrypted storage

## Bootstrap Process

1. **Initial Setup**: Admin laptop runs Docker Compose to start bootstrap services on s1
2. **Core Provisioning**: s1 provisions itself and peer core nodes (s2, s3)
3. **Cluster Formation**: Core nodes establish etcd cluster and admin services
4. **Service Deployment**: Ansible playbooks deploy storage, databases, and cluster services
5. **Local Node Expansion**: New nodes PXE boot and auto-provision based on MAC address
6. **Remote Node Expansion**: Remote boxes on the public internet run `curl https://admin.<domain>/bootstrap/wg | sudo bash -s -- --type <type>`, which allocates a cluster hostname+IP in the WG peer subnet, registers a pubkey, and waits for `ycluster wg approve <hostname>` on a core node. See the WG overlay section below.

## Network Architecture

**IP Allocation**
- Core nodes: first 3-5 storage nodes
- Storage nodes: DHCP range 10.0.0.11-30
- Compute nodes: DHCP range 10.0.0.51-70
- macOS nodes: DHCP range 10.0.0.71-90
- WireGuard peer subnet: 10.0.1.0/24 (remote nodes onboarded via wg overlay keep their type prefix but live in this /24)
- Gateway VIP: 10.0.0.254 (cluster services, also the wg server VIP)
- Storage VIP: 10.0.0.100 (storage services)
- Uplink Service VIP: Configurable per-deployment (incoming HTTP)

**Service Discovery**
- etcd maintains cluster membership and configuration
- Dynamic inventory plugin reads from etcd for Ansible
- Services discover peers through etcd watches

**Monitoring & Health**
- Comprehensive health APIs across all node types
- Cluster status monitoring with leadership tracking
- Service health checks (Ceph, DNS, certificates, Docker, Tang)
- Web dashboard for cluster visualization and management

### WireGuard Overlay

The WG overlay is the cluster's remote-node onboarding mechanism. It is strictly a *transport* layer — a wg-bootstrapped node is still classified by its normal type (compute, nvidia, macos, nas, dev) with the matching hostname prefix, only its IP lives in the `10.0.1.0/24` peer subnet instead of the cluster's `10.0.0.0/24`.

- **Server**: the active gateway-VIP holder runs `wg0` with address `10.0.1.1/24`. A 30s systemd timer (`ycluster-wg-reconcile.timer`) on every core node checks VIP ownership and brings `wg0` up or down accordingly, so the wg server follows VIP failover without coupling to the keepalived config.
- **Routing**: every cluster node gets a static `10.0.1.0/24 via 10.0.0.254 metric 1024` route. The lower-metric connected route on the wg server wins locally; everywhere else the VIP route sends peer traffic through whichever core node currently holds the server.
- **Public surface**: nginx serves a dedicated vhost on `127.0.0.2:443/80` with an explicit whitelist (`/bootstrap/wg`, `/api/wg/register`, `/api/wg/poll/*`, read-only status endpoints). Rathole is configured to forward external traffic to `127.0.0.2`, isolating public requests from the cluster-only `127.0.0.1`/`10.0.0.x` listeners. Everything outside the whitelist returns 404 to the public internet.
- **Approval**: `/api/wg/register` stores the peer as pending in etcd; an operator runs `ycluster wg approve <hostname>` on a core node, which updates etcd and triggers `wg syncconf wg0` to bring the peer online.

