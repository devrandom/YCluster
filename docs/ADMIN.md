# YCluster Admin Interface

The YCluster admin interface provides comprehensive cluster management, monitoring, and operational tools through both web and command-line interfaces.

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

The YCluster management tools are consolidated under the `ycluster` CLI, which provides a unified interface for all cluster operations.

### Getting Started
```bash
# Show all available commands
ycluster --help
```

Tab completion for bash is installed automatically by the ansible playbooks.

### Cluster Health and Status
```bash
# Comprehensive cluster health check
ycluster cluster status

# Alternative health check command
ycluster cluster health

# Populate local node information in etcd
ycluster cluster populate-local-node
```

The cluster status check shows:
- Node status and reachability
- Service health across all nodes
- Leadership status
- VIP assignments
- Certificate expiry
- Clock synchronization

### DHCP and Node Management
```bash
# List all node allocations
ycluster dhcp list allocations

# List DHCP leases
ycluster dhcp list leases

# List both allocations and leases
ycluster dhcp list all

# Delete allocation by hostname or MAC address
ycluster dhcp delete <hostname_or_mac>

# Update static DNS hosts from etcd
ycluster dhcp update-hosts
```

### TLS Certificate Management
```bash
# Generate self-signed certificate
ycluster tls generate --common-name cluster.local --san 10.0.0.254 --san localhost

# Set common name for future certificate generation
ycluster tls set-common-name your-domain.com

# Get current common name
ycluster tls get-common-name

# Set certificate from files
ycluster tls set --cert-file cert.pem --key-file key.pem

# Get current certificate information
ycluster tls get

# Delete certificate
ycluster tls delete

# Fetch certificates from etcd to nginx
ycluster tls fetch-certs
```

### HTTPS Domain Configuration
```bash
# Set primary domain
ycluster https set-domain your-domain.com

# Add domain alias
ycluster https add-alias www.your-domain.com

# Remove domain alias
ycluster https remove-alias www.your-domain.com

# Set email for Let's Encrypt
ycluster https set-email your-email@example.com

# Show current configuration
ycluster https get

# List all configured domains
ycluster https list-domains

# Delete HTTPS configuration
ycluster https delete
```

### Let's Encrypt Certificate Management
```bash
# Check certificate status and configuration
ycluster certbot status

# Obtain new certificate (test mode)
ycluster certbot obtain --test

# Obtain production certificate
ycluster certbot obtain

# Obtain certificate non-interactively
ycluster certbot obtain --non-interactive

# Renew existing certificates
ycluster certbot renew

# Renew certificates non-interactively
ycluster certbot renew --non-interactive

# List certificate details
ycluster certbot list

# Revoke certificate
ycluster certbot revoke <domain>

# Delete certificate
ycluster certbot delete <domain>

# Update nginx configuration with domain from etcd
ycluster certbot update-nginx
```

### Rathole Tunnel Configuration
```bash
# Set rathole server configuration
ycluster rathole set --remote-addr "your-server.com:2333" --token "secret_token"

# Show current configuration
ycluster rathole get

# Generate client configuration from etcd
ycluster rathole generate-client

# Generate SSH-only client configuration
ycluster rathole generate-ssh-client

# Delete configuration
ycluster rathole delete
```

### Frontend Node Management
```bash
# List frontend nodes
ycluster frontend list

# Add frontend node
ycluster frontend add f1 your-server.com --description "External server"

# Delete frontend node
ycluster frontend delete f1

# Show frontend node details
ycluster frontend show f1
```

### Storage Management
```bash
# Start user RBD volume (acquire lock and mount)
ycluster storage rbd start

# Start with LUKS passphrase instead of Clevis
ycluster storage rbd start -K

# Stop user RBD volume (unmount and release lock)
ycluster storage rbd stop

# Show current volume status
ycluster storage rbd status

# Test if volume can be decrypted
ycluster storage rbd check

# Test decryption with LUKS passphrase
ycluster storage rbd check -K

# Ensure Clevis binding is correct
ycluster storage rbd bind -k /path/to/passphrase/file
```

## Node Types and IP Allocation

### Adding Nodes

#### Storage

Provision the node via PXE boot and autoinstall.  The node will automatically be classified as a storage node based on its MAC address prefix and allocated a hostname from the `s1` to `s20` range.  You can verify that the node has been allocated an address in the expected range with `lease-manager all`.

#### MacOS

Allocate a hostname.  On a core node, run `lease-manager all` and select a new hostname from the `m1` to `m20` range.  Set this hostname on the macOS node and connect the node to the network.  You can verify that the node has been allocated an address in the expected range with `lease manager all`.

Copy the SSH public key from a core node to the macOS node to allow SSH access.

```bash
cat /var/www/html/ansible_ssh_key.pub # FIXME use curl after fixing web service
ssh user@m1
sudo su -
mkdir -p .ssh
echo "..." >> .ssh/authorized_keys
chmod 700 .ssh
chmod 600 .ssh/authorized_keys
```

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
- **DHCP Server**: Check systemd journal for dhcp-server service or health endpoint
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

## CLI Command Reference

The `ycluster` command provides comprehensive tab completion.

### Ansible Vault

Create a vault password:

```shell
set -C
touch /run/shm/.vault-pass
chmod 600 /run/shm/.vault-pass
openssl rand -base64 30 > /run/shm/.vault-pass
export ANSIBLE_VAULT_PASSWORD_FILE=/run/shm/.vault-pass
```

BACKUP THE PASSWORD FILE `/run/shm/.vault-pass` in a secure place!

```shell
ansible-vault create group_vars/all/vault.yml
# or later
ansible-vault edit group_vars/all/vault.yml
```

Put something like this in the vault file:

```yaml
vault_secrets_volume_key: "XXX"
vault_user_volume_key: "YYY"
```

where XXX and YYY can be generated with something like:

```shell
openssl rand -base64 30
```
