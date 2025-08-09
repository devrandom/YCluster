# XCluster Operations Handbook

## etcd Cluster Recovery

### Scenario 1: Single Node Failure with Cluster Quorum Intact

When a single etcd node (e.g., s2) fails but the majority of nodes (s1, s3) remain healthy, follow these steps to recover:

#### Prerequisites
- Verify cluster health from a working node:
  ```bash
  etcdctl endpoint health --cluster
  etcdctl member list
  ```
- Ensure you have quorum (2 out of 3 nodes responding)

#### Recovery Steps

1. **Remove the failed member from the cluster**
   
   From any healthy core node (s1 or s3):
   ```bash
   # List current members and identify the failed node's member ID
   etcdctl member list
   
   # Remove the failed member (replace <member-id> with actual ID)
   etcdctl member remove <member-id>
   
   # Verify removal
   etcdctl member list
   ```

2. **Reinstall the failed node**
   
   Trigger PXE boot and autoinstall for the failed node.  This will provision the base system and wipe storage.

3. **Run Ansible playbooks to rejoin the cluster**
   
   From the admin laptop:
   ```bash
   # Run the etcd installation playbook
   docker compose exec ansible ansible-playbook install-etcd.yml --limit s2
   
   # Run any additional required playbooks
   docker compose exec ansible ansible-playbook site.yml --limit s2
   ```

4. **Verify cluster recovery**
   
   From any core node:
   ```bash
   # Check cluster health
   etcdctl endpoint health --cluster
   
   # Verify all members are present
   etcdctl member list
   
   # Check cluster status
   etcdctl endpoint status --cluster --write-out=table
   ```

#### Post-Recovery Verification

- Ensure all etcd endpoints are healthy
- Verify that services dependent on etcd (DHCP, admin services) are functioning
- Check that the node has registered itself in etcd:
  ```bash
  etcdctl get --prefix /cluster/nodes/by-hostname/s2
  ```

#### Troubleshooting

- If the node fails to rejoin automatically, check etcd logs:
  ```bash
  sudo journalctl -u etcd -f
  ```

---

*Note: This procedure assumes the cluster maintains quorum throughout the recovery process. For scenarios where quorum is lost, see the disaster recovery procedures.*
