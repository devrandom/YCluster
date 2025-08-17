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

## Operational Features

**Self-Healing**
- Automatic leader election on node failures
- Ceph data rebalancing and recovery
- Service auto-restart with systemd

**Monitoring**
- Comprehensive health APIs with service status
- Web-based cluster status dashboard
- Leadership tracking and split-brain detection

**Security**
- SSH key-based authentication
- Isolated cluster network
- Controlled external access via proxy

**Maintenance**
- Rolling updates through Ansible
- Graceful service migration
- Zero-downtime cluster expansion

**Ceph Storage**
- LVM-based disk management with MicroCeph
- RBD images for PostgreSQL and Qdrant
- Pool management and cluster maintenance

**Network Services**
- Scapy-based DHCP server with etcd integration
- dnsmasq DNS/TFTP services
- Squid proxy for package caching

**Management Tools**
- Cluster health checks and node management
- Certificate and configuration management utilities

## System Architecture

### Node Types

**Core Nodes (s1, s2, s3)**
- Form the control plane with etcd cluster
- Run admin services (node allocation, DHCP, cluster status APIs)
- Provide network services (DNS, DHCP, PXE boot)
- Host Keepalived VIP for high availability
- Act as storage nodes

**Storage Nodes (s4, ...)**
- Run MicroCeph for distributed block storage
- Host stateful services (PostgreSQL, Qdrant) with leader election
- Provide RBD volumes with exclusive locking

**Compute Nodes (c\*)**
- Provide processing capacity for workloads

**Frontend Nodes (f\*)**
- Provide external access via Rathole reverse proxy
- Registered in etcd for dynamic configuration

### Core Services

**Bootstrap Services** (Docker Compose on admin laptop)
- NTP Server for time synchronization
- DNSMASQ for DHCP/DNS/PXE boot
- HTTP Server for installation media and APIs
- Squid Proxy for package caching
- Ansible Container for automation

**Cluster Services**
- **etcd**: Distributed key-value store for cluster state
- **MicroCeph**: Distributed block storage with RBD
- **Leader Election**: Ensures single active instance for stateful services
- **Keepalived**: Virtual IP failover for singleton services, such as DHCP and routing gateway

### Certificate Management
- TLS infrastructure with self-signed certificates and Let's Encrypt integration
- Certificate synchronization via etcd and nginx
- Automated renewal and distribution

### High Availability

**Service Resilience**
- etcd-based leader election for stateful services (PostgreSQL, Qdrant, DHCP)
- Automatic leader migration on node failure
- Multiple core nodes provide redundant admin services

**Storage Resilience**
- Ceph replication across storage nodes
- RBD exclusive locking prevents split-brain
- Automatic data rebalancing on node changes

**Network Resilience**
- Virtual IP failover for admin services
- Multiple DNS/DHCP servers with leader election
- Redundant proxy services on core nodes

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

