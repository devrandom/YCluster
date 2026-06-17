# etcd Operations

## TLS (mTLS)

etcd runs with mutual TLS on both the client and peer ports. Authentication is
by **client-cert possession**, never source IP (it's forgeable on the flat
`/24`). Design rationale: `docs/design/etcd-access-hardening.md`.

### Cert layout (per core node, `/etc/etcd/tls/`)

| file | what | who can read |
|---|---|---|
| `ca.crt` | cluster etcd CA (public) | world-readable |
| `server.crt` / `server.key` | shared `etcd-core` identity (server + peer) | `etcd:etcd`, key `0600` |
| `client.crt` / `client.key` | core's client identity (= the core cert) | group `etcd-client`, `0640` |

Non-core etcd clients (compute / Incus hosts) get `ca.crt` + a
`CN=etcd-client` `client.{crt,key}` in the same dir. The unified CA
(cert **and** key) also lands at `/etc/ycluster/ca/` on core nodes. All of it
is generated once by `setup-etcd-tls.yml` into the replicated
`bootstrap_files_dir` (owned by no single node, ssh-key style) and distributed
by Ansible. Certs are 1-year (CA 10-year).

### Ports

- **Plaintext** `2379` (client) / `2380` (peer) — exist only during a migration
  (phases `listen`/`connect`); removed at `enforce`.
- **TLS** `2381` (client) / `2382` (peer) — the permanent mTLS ports.

### Running etcdctl by hand

On a core node, source the managed env (sets endpoints + `ETCDCTL_CACERT/CERT/KEY`):

```bash
set -a; . /etc/ycluster/etcd-client.env; set +a
etcdctl endpoint health --cluster
```

Or explicitly:

```bash
etcdctl --endpoints https://10.0.0.11:2381 \
  --cacert /etc/etcd/tls/ca.crt --cert /etc/etcd/tls/client.crt --key /etc/etcd/tls/client.key \
  member list -w table
```

### Rotating / adding a client cert

Re-running `setup-etcd-tls.yml` is idempotent — it regenerates only what's
missing. To force reissue, delete the relevant files from `bootstrap_files_dir`
(prod: the etcd PKI dir) and re-run. To onboard a new non-core etcd client,
add its group to the distribution play in `setup-etcd-tls.yml` (the `compute`
play is the template) and run it `--limit <node>`.

## Migrating a live cluster from plaintext to mTLS

> **Already done on the prod cluster** (steady state is `enforce`). This section
> is the procedure to repeat for a rebuild, plus the lessons that procedure now
> bakes in — see "Rollout lessons" below.

Driven by the single ordered var `etcd_tls_phase`
(`off → listen → connect → enforce`). **Roll each phase out one node at a time
with `--limit`, confirming quorum between nodes** — never a fleet-wide run, or
all three etcd restart together and quorum drops. Rehearsed end-to-end on the
dev cluster by `dev/cluster/etcd-tls-migrate.sh` (mirror that script's order).

Set `etcd_tls_phase` in host_vars (or pass `-e etcd_tls_phase=<phase>`).

> **First deploy is itself a rolling step.** Older etcd configs were never
> rewritten on a node that already had data; this code always renders
> `/etc/default/etcd` from a template, so the *first* run — even at phase `off`
> — rewrites the config and restarts etcd once per node. Do that first pass
> `--limit` per node too.

For each phase, and for each node `s1`, `s2`, `s3` in turn:

```bash
# 1. distribute certs (from `listen` on) + render etcd config + restart, one node
ansible-playbook setup-etcd-tls.yml install-etcd.yml -e etcd_tls_phase=<phase> --limit s1
# 2. confirm quorum survived before touching the next node
etcdctl endpoint health --cluster        # (TLS env once past `off`)
```

After all three nodes are at the phase, converge **every etcd client** so it
follows. This is two steps, not one:

```bash
# 1. write the TLS etcd-client.env + /etc/environment fleet-wide
ansible-playbook admin/install-ycluster-package.yml -e etcd_tls_phase=<phase>
# 2. re-render the service units that consume etcd, so they pick up the
#    EnvironmentFile and RESTART onto the new endpoints. site.yml is the
#    convergence; or run the unit-defining playbooks (setup-web-services,
#    install-certbot, admin-stats, setup-network-services, setup-wg,
#    install-dhcp-leader-election, monitoring/install-prometheus,
#    app/install-local-ai-proxy, storage/install-storage-leader-election).
ansible-playbook site.yml -e etcd_tls_phase=<phase> --limit <node>
```

> **Writing the env file is not enough.** A unit only reads
> `/etc/ycluster/etcd-client.env` if it has `EnvironmentFile=` *and* is
> restarted **after** both the file and the unit are in place — a running
> process keeps its old (plaintext) environment. Re-run the unit-defining
> playbooks so the units are converted and restarted. `local-ai-proxy` is a
> compiled Go binary: it must be **rebuilt and redeployed**
> (`app/install-local-ai-proxy.yml`) — it reads the same `ETCD_*` env.
> Oneshot/timer units (`update-dhcp-hosts`, `ycluster-wg-reconcile`,
> `update-blackbox-targets`) do **not** read `/etc/environment`, so they need
> `EnvironmentFile=` explicitly — verify with `systemctl cat <unit>`.

Phase notes:
- **`listen`** — additive (adds the TLS listeners); safe, no client/peer change.
- **`connect`** — switches clients to TLS and runs `etcdctl member update` to
  flip each node's advertised peer URL to `https`. Safe only because every node
  reached `listen` first. Verify peer URLs flipped: `etcdctl member list`.
- **`enforce`** — drops the plaintext listeners and turns on `client-cert-auth`.
  Only advance here once **every** node is at `connect` (all peer URLs `https`).

Don't skip phases or advance a phase before the previous one is on all nodes —
the ordering is what preserves quorum. Issue client certs for the real Incus
hosts (nv2/nv3) before they need etcd at/after `connect`.

## Cluster Recovery

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

*Note: This procedure assumes the cluster maintains quorum throughout the recovery process. For quorum loss (two of three members down) and restore-from-snapshot, see [disaster-recovery.md → etcd quorum loss](disaster-recovery.md#scenario-etcd-quorum-loss).*
