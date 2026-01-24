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
5. **Node Expansion**: New nodes PXE boot and auto-provision based on MAC address

## Network Architecture

**IP Allocation**
- Core nodes: first 3-5 storage nodes
- Storage nodes: DHCP range 10.0.0.11-30
- Compute nodes: DHCP range 10.0.0.51-70
- macOS nodes: DHCP range 10.0.0.71-90
- Gateway VIP: 10.0.0.254 (cluster services)
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

