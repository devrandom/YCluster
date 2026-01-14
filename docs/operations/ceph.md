# Ceph Operations

## Cluster Recovery

### Scenario: Single Storage Node Failure

When a storage node (e.g., s2) fails and needs to be replaced, follow these steps to recover:

#### Prerequisites
- Verify cluster health from a working storage node:
  ```bash
  ceph status
  ```

#### Recovery Steps

1. **Remove the failed node from the MicroCeph cluster**

   From any healthy storage node (s1 or s3):
   ```bash
   # Remove the failed node from the cluster (replace s2 with actual failed node)
   microceph cluster remove -f s2

   # Verify removal
   microceph cluster list
   ```

2. **Reinstall the failed node**

   Trigger PXE boot and autoinstall for the failed node. This will provision the base system and wipe storage.

3. **Run Ansible playbooks to rejoin the cluster**

   From any core node:
   ```bash
   # Run the add-ceph-nodes playbook to rejoin the node
   ansible-playbook add-ceph-nodes.yml
   ansible-playbook setup-ceph-disk.yml --limit s2
   ```

4. **Verify cluster recovery**

   From any storage node:
   ```bash
   # Check cluster health
   sudo ceph status

   # Verify all nodes are present
   microceph cluster list

   # Check OSD status
   sudo ceph osd tree

   # Monitor recovery progress
   sudo ceph -w
   ```

#### Post-Recovery Verification

- Ensure all OSDs are up and in
- Verify that data rebalancing completes successfully
- Check that the node has registered itself in etcd:
  ```bash
  etcdctl get --prefix /cluster/nodes/by-hostname/s2
  ```

#### Troubleshooting

- If the node fails to rejoin automatically, check MicroCeph logs:
  ```bash
  sudo journalctl -u snap.microceph.daemon -f
  ```
- Monitor Ceph recovery progress:
  ```bash
  sudo ceph health detail
  ```
- If you get a "UNIQUE constraint failed: core_token_records.name" error when trying to add a node:
  ```bash
  # Check for stale token records
  microceph cluster sql "SELECT * FROM core_token_records;"

  # Remove stale token record (replace s2 with actual node name)
  microceph cluster sql "DELETE FROM core_token_records WHERE name='s2';"

  # Then retry adding the node
  microceph cluster add s2
  ```
- If you get a "This 'config' entry already exists" error when trying to join a node:
  ```bash
  # Check for stale config entries
  microceph cluster sql "SELECT * FROM config WHERE key LIKE 'mon.host.%';"

  # Remove stale config entry (replace s2 with actual node name)
  microceph cluster sql "DELETE FROM config WHERE key='mon.host.s2';"

  # Then retry the join process
  microceph cluster add s2
  # On the target node:
  microceph cluster join <token>
  ```

---

*Note: This procedure assumes the cluster maintains quorum throughout the recovery process. For scenarios where quorum is lost, see the disaster recovery procedures.*
