# XCluster Architecture

## Overview

XCluster is a self-bootstrapping infrastructure platform that creates a highly available cluster from bare metal servers. The system provides automated provisioning, distributed storage, and service orchestration with built-in failover capabilities.

## Core Components

### Bootstrap Layer
The bootstrap process begins from an admin laptop connected to the first core node (s1). Docker Compose orchestrates the initial services needed for network booting and provisioning:

- **NTP Server**: Provides time synchronization for the entire cluster
- **DNSMASQ**: Handles DHCP, DNS, and PXE boot services for network provisioning
- **HTTP Server**: Serves installation media, autoinstall configurations, and APIs
- **Squid Proxy**: Caches packages and updates to reduce external bandwidth
- **Ansible Container**: Executes infrastructure automation playbooks

### Node Types

#### Core Nodes (s1, s2, s3)
Core nodes form the control plane and provide administrative services:
- **etcd Cluster**: Distributed key-value store for cluster state and coordination
- **Admin Services**: Node allocation API, DHCP management, and provisioning services
- **Keepalived VIP**: High availability virtual IP for admin services
- **Network Services**: DNS, DHCP, and PXE boot infrastructure

#### Storage Nodes
Storage nodes provide distributed block storage and run stateful services:
- **MicroCeph**: Distributed storage cluster providing RBD (RADOS Block Device) volumes
- **PostgreSQL**: Database service with RBD-backed storage and leader election
- **Qdrant**: Vector database service with RBD-backed storage and leader election

#### Compute Nodes
Compute nodes provide processing capacity for workloads (architecture supports but not fully implemented in current playbooks).

### High Availability Design

#### Service Leadership
Stateful services use etcd-based leader election to ensure only one active instance:
- PostgreSQL runs on exactly one storage node at a time
- Qdrant runs on exactly one storage node at a time
- Leaders can migrate between nodes automatically on failure

#### Storage Resilience
- Ceph provides distributed, replicated block storage
- RBD volumes use exclusive locking to prevent split-brain scenarios
- Automatic failover and recovery of storage services

#### Network Resilience
- Keepalived provides VIP failover for admin services
- Multiple core nodes can serve admin functions
- DNS and DHCP services remain available during node failures

### Data Flow

#### Node Provisioning
1. New nodes PXE boot from core node services
2. Autoinstall process configures base system
3. Ansible playbooks install and configure services
4. Node registers itself in etcd cluster state

#### Service Discovery
- etcd maintains authoritative cluster membership
- Dynamic inventory plugin reads node allocations from etcd
- Services discover peers through etcd key-value store

#### Storage Access
- Applications request RBD volumes from Ceph cluster
- Exclusive locks prevent concurrent access
- Leader election ensures single writer per volume

## Network Architecture

### IP Allocation
- Core nodes use static IP assignments
- Storage and compute nodes receive DHCP assignments
- Admin services accessible via virtual IP
- All nodes participate in cluster-wide DNS

### Service Communication
- etcd provides cluster coordination on standard ports
- Ceph uses standard ports for storage communication
- Admin APIs accessible through nginx reverse proxy
- Internal services communicate over cluster network

## Deployment Patterns

### Initial Bootstrap
1. Admin laptop connects to s1 via direct network
2. Docker Compose starts bootstrap services
3. s1 provisions itself and additional core nodes
4. Core nodes establish etcd cluster and admin services

### Cluster Expansion
1. New nodes PXE boot from existing infrastructure
2. Automatic node type detection based on MAC address
3. Ansible playbooks configure nodes based on type
4. Services automatically discover and integrate new capacity

### Service Management
- All services managed through systemd
- Leader election services start/stop dependent services
- Ansible playbooks provide declarative configuration
- Rolling updates possible through playbook execution

## Security Model

### Access Control
- SSH key-based authentication for all nodes
- Ansible manages configuration through privileged access
- Service-to-service communication over trusted network

### Network Isolation
- Cluster operates on isolated network segment
- Proxy provides controlled external access
- Internal services bind to cluster interfaces only

## Operational Characteristics

### Self-Healing
- Leader election automatically recovers from node failures
- Ceph rebalances data when nodes join/leave
- Services restart automatically on transient failures

### Monitoring and Observability
- etcd provides cluster health and membership status
- Service logs available through systemd journal
- Ceph provides storage cluster health monitoring

### Maintenance Operations
- Rolling updates through Ansible playbooks
- Graceful service migration during maintenance
- Cluster can operate with reduced capacity during updates

This architecture provides a foundation for building resilient, self-managing infrastructure that can scale from a few nodes to larger deployments while maintaining high availability and operational simplicity.
