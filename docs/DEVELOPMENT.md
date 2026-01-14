# YCluster Development Guide

## Admin API Development

Run the admin API locally without a full cluster using Docker Compose:

```bash
# Start the dev environment
docker compose -f docker-compose.dev.yaml up -d

# View logs
docker compose -f docker-compose.dev.yaml logs -f admin-api

# Stop
docker compose -f docker-compose.dev.yaml down
```

This starts:
- **etcd** on port 2379
- **admin-api** on port 12723
- **etcd-init** seeds test data (DHCP lease for localhost)

### Available Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Allocation counts by node type |
| `GET /api/allocations` | List all node allocations |
| `GET /api/allocate?mac=XX:XX:XX:XX:XX:XX` | Allocate hostname/IP for MAC |
| `GET /api/health` | Comprehensive health check |
| `GET /macos/bootstrap` | macOS bootstrap script |
| `GET /autoinstall/user-data` | Ubuntu autoinstall cloud-init |
| `GET /status` | Web UI dashboard |

### Testing Bootstrap Endpoints

The dev environment seeds a test DHCP lease mapping the Docker bridge IP to a macOS MAC address. This allows testing bootstrap endpoints directly:

```bash
# Test macOS bootstrap script
curl http://localhost:12723/macos/bootstrap

# Test with different node types by updating the lease
docker exec etcd-dev etcdctl put /cluster/dhcp/leases/docker-bridge \
  '{"ip": "172.18.0.1", "mac": "58:47:ca:11:22:33"}'  # Storage node

docker exec etcd-dev etcdctl put /cluster/dhcp/leases/docker-bridge \
  '{"ip": "172.18.0.1", "mac": "aa:bb:cc:dd:ee:ff"}'  # Compute node
```

### Live Reload

Source files are mounted as volumes. After editing, restart the container:

```bash
docker compose -f docker-compose.dev.yaml restart admin-api
```
