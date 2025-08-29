# XCluster Admin Interface

The XCluster admin interface provides comprehensive cluster management, monitoring, and operational tools through both web and command-line interfaces.

## Web Dashboard

Access the admin dashboard at `https://admin.your-domain.com/status` or `https://10.0.0.254/status`

### Dashboard Features

#### Real-time Monitoring
- **Auto-refresh**: Configurable intervals (5 seconds to 5 minutes)
- **Live updates**: Visual indicators for data refresh and errors
- **Responsive design**: Works on desktop and mobile devices

#### Node Status Overview
- **Node inventory**: All cluster nodes with type, IP, and status
- **Health monitoring**: Per-service health checks for each node
- **Leadership tracking**: Current storage and DHCP leaders
- **Drain management**: Ability to drain/undrain nodes for maintenance

#### Service Health Monitoring
The dashboard monitors these services across all nodes:
- **etcd**: Cluster coordination and configuration storage
- **Ceph**: Distributed storage health and status
- **PostgreSQL**: Database service (on storage leader only)
- **Qdrant**: Vector database service (on storage leader only)
- **Docker**: Container runtime and registry
- **DNS/DHCP**: Network services and leader election
- **TLS Certificates**: Expiry monitoring and renewal status
- **Clock Synchronization**: NTP sync and skew detection
- **VIP Status**: Virtual IP failover status
- **Rathole**: Reverse proxy for external access

#### Virtual IP (VIP) Management
- **Gateway VIP** (10.0.0.254): Cluster services and external access
- **Storage VIP** (10.0.0.100): Storage services like Docker registry
- **Keepalived Status**: Service status across all nodes
- **Failover Monitoring**: Active node tracking and interface status

#### Certificate Management
- **Expiry Tracking**: Days until certificate expiration
- **Status Monitoring**: Valid, warning, critical, or expired states
- **Automatic Renewal**: Integration with Let's Encrypt
- **Subject Information**: Certificate details and issuer

#### Clock Synchronization
- **NTP Monitoring**: Clock offset detection across all nodes
- **Skew Alerts**: Warning and critical thresholds
- **Sync Status**: Per-node synchronization health

## API Endpoints

The admin service provides REST APIs for programmatic access:

### Node Management
- `GET /api/allocations` - List all node allocations
- `GET /api/status` - Get allocation counts by node type
- `POST /api/allocate?mac=<mac>` - Allocate hostname for MAC address

### Health Monitoring
- `GET /api/health` - Comprehensive health check for current node
- `GET /api/cluster-status` - Cluster-wide health status (JSON)
- `GET /api/ping` - Simple connectivity test
- `GET /api/time` - Current timestamp for sync checks

### Node Drain Management
- `POST /api/drain` - Drain current node
- `POST /api/undrain` - Undrain current node
- `POST /api/drain/<hostname>` - Drain specific node
- `POST /api/undrain/<hostname>` - Undrain specific node
- `GET /api/drain/status` - Check drain status of current node
- `GET /api/drain/status/<hostname>` - Check drain status of specific node

### Configuration Generation
- `GET /api/dhcp-config` - Generate DHCP static host configuration
- `GET /api/hosts` - Generate /etc/hosts format entries

### Autoinstall Support
- `GET /autoinstall/user-data` - Dynamic cloud-init configuration
- `GET /autoinstall/meta-data` - Empty meta-data for autoinstall

## Command Line Tools

### Cluster Health
```bash
check-cluster
```
Comprehensive cluster health check showing:
- Node status and reachability
- Service health across all nodes
- Leadership status
- VIP assignments
- Certificate expiry
- Clock synchronization

### Node Management
```bash
# List all DHCP allocations
lease-manager list

# Delete allocation by hostname
lease-manager delete-hostname <hostname>

# Delete allocation by MAC address
lease-manager delete-mac <mac>
```

### Certificate Management
```bash
# Check certificate status
certbot-manager status

# Obtain new certificate (test mode)
certbot-manager obtain --test-cert

# Obtain production certificate
certbot-manager obtain

# Renew existing certificates
certbot-manager renew

# List certificate details
certbot-manager list

# Update nginx configuration
certbot-manager update-nginx
```

### HTTPS Configuration
```bash
# Set primary domain
https-config set-domain your-domain.com

# Set email for Let's Encrypt
https-config set-email your-email@example.com

# Add domain alias
https-config add-alias www.your-domain.com

# Remove domain alias
https-config remove-alias www.your-domain.com

# Show current configuration
https-config show
```

### Frontend Node Management
```bash
# List frontend nodes
frontend-manager list

# Add frontend node
frontend-manager add f1 your-server.com --description "External server"

# Remove frontend node
frontend-manager delete f1

# Show frontend node details
frontend-manager show f1
```

### Rathole Configuration
```bash
# Set rathole server configuration
rathole-config set --remote-addr "your-server.com:2333" --token "secret_token"

# Show current configuration
rathole-config show

# Generate client configuration
rathole-config generate-client

# Delete configuration
rathole-config delete
```

## Node Types and IP Allocation

### Automatic Node Detection
Nodes are automatically classified based on MAC address prefixes:
- **Storage nodes** (s1-s20): MAC addresses starting with `58:47:ca`
- **Compute nodes** (c1-c20): All other MAC addresses
- **macOS nodes** (m1-m20): Manual classification available

### IP Address Ranges
- **Storage**: 10.0.0.11-30 (s1-s20)
- **Compute**: 10.0.0.51-70 (c1-c20)
- **macOS**: 10.0.0.71-90 (m1-m20)
- **AMT interfaces**: 10.10.10.x (hostname + 'a' suffix)

### Interface Configuration
Each node type has predefined network interface mappings:

**Storage Nodes:**
- Cluster: `enp2s0f0np0`
- Uplink: `enp87s0`
- AMT: `enp89s0`

**Compute Nodes:**
- Cluster: `enp1s0f0`
- Uplink: `enp1s0f1`
- AMT: `enp1s0f2`

**macOS Nodes:**
- Cluster: `en0`
- Uplink: `en1`
- AMT: `en2`

## Leadership and High Availability

### Storage Leader
The storage leader runs:
- PostgreSQL database
- Qdrant vector database
- Docker registry
- Rathole client (for external access)

### DHCP Leader
The DHCP leader runs:
- DHCP server with health monitoring
- Dynamic hostname allocation
- Lease management

### Leader Election
- **etcd-based**: Uses etcd for distributed leader election
- **Automatic failover**: Leaders are automatically elected on failure
- **Drain support**: Nodes can be drained to prevent leader election
- **Health monitoring**: Continuous health checks ensure leader availability

## Maintenance Operations

### Node Draining
Draining a node prevents it from becoming a leader and allows for maintenance:

```bash
# Drain a node (via web interface or API)
curl -X POST https://admin.your-domain.com/api/drain/s1

# Undrain a node
curl -X POST https://admin.your-domain.com/api/undrain/s1
```

### Certificate Renewal
Certificates are automatically monitored and can be renewed:

```bash
# Check certificate status
certbot-manager status

# Force renewal
certbot-manager renew --non-interactive
```

### Service Health Monitoring
Each service has specific health check criteria:
- **Healthy**: Service running and functional
- **Degraded**: Service running but with issues
- **Unhealthy**: Service not running or failing
- **Not Required**: Service not needed on this node
- **Error**: Health check failed

## Troubleshooting

### Common Issues

**Split-brain Detection:**
Services running on non-leader nodes are flagged as unhealthy to prevent split-brain scenarios.

**Clock Skew:**
- Warning: >100ms offset
- Critical: >1000ms offset
- Uses NTP protocol to check against VIP (10.0.0.254)

**Certificate Expiry:**
- Warning: <30 days until expiry
- Critical: <7 days until expiry
- Expired: Certificate has expired

**VIP Failover:**
Virtual IPs automatically fail over between nodes using keepalived. The dashboard shows which node currently holds each VIP.

### Log Locations
- **Admin API**: Check systemd journal for admin-api service
- **DHCP Server**: `/var/log/dhcp-server.log`
- **Leader Election**: Check systemd journal for leader election services
- **Certificate Management**: Check systemd journal for certbot services

## Security

### TLS Certificates
- **Self-signed**: Generated automatically for initial setup
- **Let's Encrypt**: Production certificates with automatic renewal
- **Storage**: Certificates stored in etcd for cluster-wide access

### Network Security
- **Internal communication**: Services communicate over cluster network
- **External access**: Controlled via rathole reverse proxy
- **AMT interfaces**: Separate network segment for out-of-band management

### Access Control
- **SSH keys**: Automatically deployed during node provisioning
- **Service isolation**: Services run with appropriate user privileges
- **Network segmentation**: Separate networks for different traffic types
