# XCluster Architecture

## Overview

XCluster is a self-bootstrapping infrastructure platform that creates highly available clusters from bare metal servers. It provides automated provisioning, distributed storage, and service orchestration with built-in failover.

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

### Certificate Management
- TLS infrastructure with self-signed certificates and Let's Encrypt integration
- Certificate synchronization via etcd and nginx
- Automated renewal and distribution

### High Availability

- **Leader Election**: etcd-based failover for PostgreSQL, Qdrant, DHCP
- **Storage**: Ceph replication, RBD exclusive locking, automatic rebalancing
- **Network**: VIP failover, redundant DNS/DHCP/proxy services

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
- Admin VIP: 10.0.0.254

**Service Discovery**
- etcd maintains cluster membership and configuration
- Dynamic inventory plugin reads from etcd for Ansible
- Services discover peers through etcd watches

