# Disaster Recovery

Runbooks for the failure modes the per-service docs punt on: **loss of quorum**
(etcd or Ceph), full cold-start, VIP split-brain, and restoring databases from
backup. For single-node-failure-with-quorum-intact, see
[`etcd.md`](etcd.md) and [`ceph.md`](ceph.md) — those are recoveries, not
disasters.

> **The one unrecoverable mode is etcd quorum loss with no usable survivor and
> no snapshot.** etcd is the single source of truth; everything else
> (Ceph layout, leader election, DHCP/DNS, inference config, authentik secrets)
> is rebuildable from etcd + Ansible. Protect the etcd snapshot accordingly.

## Contents

- [What is backed up](#what-is-backed-up)
- [Age key custody](#age-key-custody)
- [Restoring a single database](#restoring-a-single-database) (postgres / qdrant / etcd)
- [Scenario: etcd quorum loss](#scenario-etcd-quorum-loss)
- [Scenario: Ceph quorum loss or corruption](#scenario-ceph-quorum-loss-or-corruption)
- [Scenario: full cluster cold-start](#scenario-full-cluster-cold-start)
- [Scenario: VIP split-brain](#scenario-vip-split-brain)
- [Restoring authentik](#restoring-authentik)
- [Ceph off-cluster backup story](#ceph-off-cluster-backup-story)
- [Verification drills](#verification-drills)

---

## What is backed up

`backup-databases` (`storage/scripts/backup-databases`, daily timer on the
storage leader, installed by `storage/setup-user-rbd.yml`) dumps three engines,
encrypts each with `age` to the configured recipients, and rsyncs the encrypted
copies to every enabled offsite destination.

| Engine | Captures | Plaintext path | Encrypted path |
|---|---|---|---|
| PostgreSQL (`pg_dumpall`) | all roles + DBs — open-webui, **authentik**, etc. | `/rbd/user/backups/postgres/*.sql.gz` | `…/encrypted/postgres/*.sql.gz.age` |
| Qdrant (full snapshot) | all collections (vector store) | `/rbd/user/backups/qdrant/*.snapshot` | `…/encrypted/qdrant/*.snapshot.age` |
| etcd (`snapshot save`) | **all cluster state + secrets** | `/rbd/user/backups/etcd/*.db` | `…/encrypted/etcd/*.db.age` |

Recipients and offsite destinations are in etcd, managed by the CLI:

```bash
ycluster backup recipients list
ycluster backup destinations list
backup-databases status        # newest backup per engine, sizes, sync state
```

**Not captured by the database backups** (rebuilt, not restored):

- **Ceph RBD volume contents** (`/rbd/user`, `/rbd/misc`) — the RBD pools are
  the *primary* copy and have **no off-cluster snapshot** today. See
  [Ceph off-cluster backup story](#ceph-off-cluster-backup-story). The DB
  backups live *on* `/rbd/user` but are also pushed offsite, so they survive
  RBD loss. The remaining at-risk content is non-DB files on `/rbd/misc` —
  chiefly the **docker registry blobs** (`/rbd/misc/docker-registry`, the built
  open-webui images: rebuildable, but at cost) and anything an operator parked
  on the volumes by hand. (authentik's `/rbd/misc/authentik` is just
  Ansible-deployed branding — reproducible, not a gap.)
- **Everything Ansible renders** — service configs, blueprints, certs issued by
  the cluster CA. Recovered by re-running playbooks, not from backup.

### Freshness / restorability signals

The nightly **verify-restore** drill (`backup-databases verify-restore`,
`storage/setup-backup-verify.yml`) loads the newest backups into throwaway
scratch instances and writes node-exporter textfile metrics
(`/var/lib/prometheus/node-exporter/backup_restore.prom`). Alerts in
`monitoring/templates/ycluster-alerts.yml.j2`:

- `BackupRestoreFailed` (critical) — a backup did not load cleanly.
- `BackupVerifyStale` (warning) — the drill stopped running (>48h).
- `BackupStale` (warning) — a backup is >48h old (the daily backup is failing).

---

## Age key custody

Backups are encrypted to a set of **recipients** (age public keys in etcd at
`/cluster/backup/recipients/`). Decryption needs the matching **private key**.
Three classes of identity, by design:

1. **Operator offsite keys.** The real DR keys. Each operator holds an age
   identity (ideally hardware-backed, `age-plugin-yubikey`) whose public key is
   a registered recipient. The private keys live **off-cluster** (in operators'
   custody, never on a cluster node). This is what recovers backups after the
   storage leader — and its on-disk key — is lost or compromised.
2. **The nightly verify-restore identity.** A software age key on the storage
   leader at `/rbd/user/backups/.restore-identity/identity.age`
   (`root:root 0400`), created and registered by `setup-backup-verify.yml`. It
   lives on the **same encrypted RBD as the live databases**, so it adds no
   blast radius (an attacker with that key already has the plaintext DBs). It
   exists only so the automated drill can decrypt unattended. **Do not** treat
   it as a DR key — it dies with the storage leader.
3. **Recipient list = who can decrypt.** Adding/removing a recipient only
   affects *future* backups; old `.age` files are still readable only by the
   keys they were encrypted to. Rotate by adding the new recipient, waiting one
   retention window (30 days) so all live backups include it, then removing the
   old one.

**Custody to document off-repo** (do not commit private keys or their
locations): who holds each operator key, where the offsite copies live, and the
reissue procedure for when an operator leaves (remove their recipient, rotate).

Restore with an explicit identity:

```bash
backup-databases restore postgres /path/to/postgres_TIMESTAMP.sql.gz.age \
  --identity /path/to/operator-identity.age --yes
# or decrypt by hand:
age -d -i operator-identity.age postgres_TIMESTAMP.sql.gz.age | gunzip | less
```

---

## Restoring a single database

`backup-databases restore <engine> [file]` restores the **newest** backup (or an
explicit `file`) into the **live** service. It is destructive and prompts unless
`--yes` is given. Run on the storage leader.

### PostgreSQL

```bash
backup-databases restore postgres            # newest, prompts first
```

Loads a `pg_dumpall` script (roles + every database) into `postgresql@16-main`.
Cleanest onto an empty cluster; onto a populated one expect benign
"role already exists" notices (tolerated). After restore, **bounce the apps**
that cache connections/state — `open-webui`, `authentik` — on the leader.

### Qdrant

```bash
backup-databases restore qdrant
```

Stops qdrant, moves the current storage aside to
`/rbd/user/qdrant/storage.pre-restore.<ts>` (kept for rollback), extracts the
full snapshot, restarts qdrant. **First-use caveat:** verify the extracted
layout against the running qdrant version before trusting it — the snapshot is a
tar of the storage dir, but check `collections/` appears under
`/rbd/user/qdrant/storage` after restart, and that `GET /collections` lists what
you expect. Roll back by stopping qdrant and restoring the `.pre-restore.` dir.

### etcd

etcd restore is a **safe primitive only** — it restores a snapshot to a fresh
directory and never touches the live `/var/lib/etcd`, because rebuilding a quorum
is a coordinated multi-node procedure (next section).

```bash
backup-databases restore etcd --data-dir /var/lib/etcd-restore
# add --name/--initial-cluster/--peer-url for a full cluster rebuild
```

---

## Scenario: etcd quorum loss

etcd needs **2 of 3** members. Losing two simultaneously (or the dqlite/etcd
data on two) stops the cluster: no leader election, no DHCP/DNS leader, no config
reads. Pick the path by what survived.

### Confirm the situation

```bash
set -a; . /etc/ycluster/etcd-client.env; set +a
etcdctl endpoint status --cluster -w table   # which members answer, who has the highest revision
etcdctl endpoint health --cluster
```

### Path A — one member survived with intact data (preferred)

Rebuild a new single-node cluster from the survivor's *existing data* (no
snapshot needed — zero data loss), then re-add the others as fresh members.

1. **Stop etcd everywhere** so nothing competes:
   ```bash
   for h in s1 s2 s3; do ssh $h systemctl stop etcd; done
   ```
2. **On the survivor** (say `s1`), force a new single-member cluster from its
   data dir. Temporarily set `ETCD_FORCE_NEW_CLUSTER=true` in
   `/etc/default/etcd`, start etcd, confirm it is healthy and holds the data,
   then **remove the flag** and restart:
   ```bash
   ssh s1 'sed -i "/ETCD_FORCE_NEW_CLUSTER/d" /etc/default/etcd; \
           echo ETCD_FORCE_NEW_CLUSTER=true >> /etc/default/etcd; systemctl start etcd'
   ssh s1 'set -a; . /etc/ycluster/etcd-client.env; set +a; etcdctl member list -w table; etcdctl get --prefix /cluster/nodes/ | head'
   ssh s1 'sed -i "/ETCD_FORCE_NEW_CLUSTER/d" /etc/default/etcd; systemctl restart etcd'
   ```
   `s1` is now a healthy 1-node cluster with all data.
3. **Re-add the other members as fresh nodes.** For each (`s2`, `s3`): wipe its
   stale data and rejoin — `install-etcd.yml` joins an existing cluster when the
   data dir is empty:
   ```bash
   ./run-playbook.sh wipe-etcd.yml --limit s2          # destroys ONLY s2's local etcd data
   ./run-playbook.sh install-etcd.yml --limit s2       # joins the s1 cluster
   ```
   Re-add one at a time, confirming `etcdctl endpoint health --cluster` regains
   each member before the next.
4. **Converge clients** so nothing is stuck on dead endpoints:
   `./run-playbook.sh site.yml` (or at least the etcd-consuming unit playbooks —
   see `etcd.md`).

### Path B — no usable survivor (total loss / corruption): restore from snapshot

Accepts data loss back to the newest etcd snapshot (≤24h with the daily timer).

1. **Get the newest snapshot.** If `/rbd/user` survived:
   `/rbd/user/backups/etcd/<newest>.db`. Otherwise decrypt the newest offsite
   `*.db.age` with an operator key (the RBD is gone, so this is the only copy):
   ```bash
   age -d -i operator-identity.age etcd_NEWEST.db.age > /tmp/etcd-snap.db
   ```
2. **Restore the *same* snapshot on all three nodes**, each with its own
   identity but the same membership. Core node IPs: `s1=10.0.0.11`,
   `s2=10.0.0.12`, `s3=10.0.0.13`; TLS peer port `2382`:
   ```bash
   IC="s1=https://10.0.0.11:2382,s2=https://10.0.0.12:2382,s3=https://10.0.0.13:2382"
   # on s1:
   backup-databases restore etcd /tmp/etcd-snap.db --data-dir /var/lib/etcd-new \
     --name s1 --initial-cluster "$IC" --peer-url https://10.0.0.11:2382
   # on s2: --name s2 --peer-url https://10.0.0.12:2382   (same --initial-cluster, same snapshot)
   # on s3: --name s3 --peer-url https://10.0.0.13:2382
   ```
3. **Swap each restored dir into place** and start, on every node:
   ```bash
   systemctl stop etcd
   mv /var/lib/etcd /var/lib/etcd.lost && mv /var/lib/etcd-new /var/lib/etcd
   chown -R etcd:etcd /var/lib/etcd && chmod 700 /var/lib/etcd
   systemctl start etcd
   ```
4. **Verify** `etcdctl endpoint health --cluster` and that
   `/cluster/...` keys are present, then converge clients (`site.yml`).

> The TLS certs are keyed to CN/SAN, not cluster ID, so they survive a restore.
> A snapshot restore creates a **new cluster ID** — that is expected.

---

## Scenario: Ceph quorum loss or corruption

MicroCeph's mon/dqlite also needs **2 of 3**. With one mon left, Ceph I/O hangs;
RBD volumes (`/rbd/user`, `/rbd/misc`) become unavailable.

1. **Triage** (from a node where the snap still responds):
   ```bash
   ceph -s ; ceph health detail
   microceph cluster list
   snap logs microceph | tail -50          # dqlite errors => mon quorum issue
   ```
   If `ceph -s` works but dqlite logs errors, it is a daemon/quorum problem, not
   data loss — restart `snap.microceph.daemon` on all nodes (see
   `CLAUDE.md` → MicroCeph Issues) and re-check before anything drastic.

2. **Two mons lost, one survivor, data intact on OSDs.** Recover mon quorum from
   the survivor (MicroCeph monmap recovery), then re-add the other two with
   `add-ceph-nodes.yml` + `setup-ceph-disk.yml` (see `ceph.md`). Do **not** wipe
   OSD disks — the data lives there.

3. **OSD data lost / pool corruption (true disaster).** The RBD pools are the
   primary copy and are not snapshotted off-cluster, so RBD *contents* cannot be
   restored — but the **databases can**, because their backups are pushed
   offsite. Recovery shape:
   1. Rebuild Ceph empty (PXE reinstall the storage nodes as needed,
      `storage/storage.yml`).
   2. Recreate the RBD volumes: `storage/setup-user-rbd.yml`,
      `storage/setup-misc-rbd.yml`.
   3. Bring up the storage leader so the volumes mount and services start.
   4. Restore databases from the newest offsite backups
      ([Restoring a single database](#restoring-a-single-database)), then
      [restore authentik](#restoring-authentik).
   5. Re-run `site.yml` to reconverge everything Ansible owns.

   What is permanently lost in this case: anything that lived **only** as files
   on RBD and not in a backed-up database — the docker registry blobs
   (`/rbd/misc/docker-registry`; rebuildable by re-pushing images) and any files
   an operator parked on the volumes by hand. authentik's media is just
   Ansible-deployed branding, so it re-deploys with the playbook. Closing this
   gap is the [Ceph off-cluster backup story](#ceph-off-cluster-backup-story).

---

## Scenario: full cluster cold-start

Whole cluster powered off (power loss, site move). A full simultaneous power
cycle does **not** self-heal — it deadlocks on Tang, and a sysop with the
secrets-volume passphrase must break it by hand. Understand the chain first.

### The Tang bootstrap deadlock

There are two encryption layers, and they form a cross-node cycle:

- The `/rbd/*` volumes Clevis-unlock against **2-of-3 Tang** servers on the core
  nodes. Tang serves its keys from `/secrets/tang`, on a small per-node
  **encrypted "secrets" volume** — so Tang can't serve until `/secrets` is open.
- Each node's `/secrets` volume is itself Clevis-bound to the **other two**
  nodes' Tang (`t:2`, self excluded). `tang-server.service` opens `/secrets` in
  its `ExecStartPre` via **Clevis** — never the passphrase.

So when all three nodes boot cold at once, no node can open `/secrets` (the
peers' Tang isn't up either), every `tang-server.service` `ExecStartPre` fails,
and nothing downstream can unlock. **etcd quorum is not the first domino — Tang
quorum is.** The only key outside this cycle is the LUKS **passphrase** in
slot 0 (vault), which sysops hold. (A *partial* outage — one node down, two Tang
still serving — auto-heals on reboot and needs none of this.)

### Break the deadlock, then bring up in order

1. **Seed Tang quorum by passphrase.** On **at least two** core nodes, open the
   secrets volume with the passphrase (slot 0, independent of Tang) and start
   Tang:
   ```bash
   /usr/local/bin/secrets-volume-manager -K start   # passphrase-unlock + mount /secrets
   systemctl restart tang-server                     # ExecStartPre now sees /secrets mounted
   ```
   Verify each is advertising: `curl -s http://localhost:8777/adv | head -c 80`.
   Two up = `t:2` quorum; the third node's `/secrets` (and Tang) then
   Clevis-unlocks on its own, or `-K` it too.
2. **etcd quorum.** With the nodes up, confirm
   `etcdctl endpoint health --cluster`.
3. **Ceph quorum.** Confirm `ceph -s` reaches `HEALTH_OK`/`WARN` with mons up.
4. **Storage leader / RBD.** Leader election mounts `/rbd/user` + `/rbd/misc`
   (Clevis auto-unlocks now that 2-of-3 Tang serve). Confirm
   `mountpoint /rbd/user`. If a volume still won't unlock, force it with the
   passphrase: `ycluster storage rbd start -K` (user volume; vault passphrase).
5. **Apps + VIPs.** `ycluster-apps.target`, registry, rathole, and the gateway
   (10.0.0.254) / storage (10.0.0.100) VIPs follow the leader. Verify with
   `ycluster cluster status`.
6. **Remaining nodes** (compute, GPU, etc.) PXE/boot and rejoin via DHCP/etcd.

If etcd will not form quorum once the nodes are up (corrupt data on two nodes),
drop into [etcd quorum loss](#scenario-etcd-quorum-loss) — but Tang quorum
(step 1) comes first regardless, or `/secrets` never opens.

---

## Scenario: VIP split-brain

Two nodes both claim a keepalived VIP (gateway `10.0.0.254` or storage
`10.0.0.100`) — usually after a partition heals with VRRP still blocked, or
config drift (mismatched `virtual_router_id`/auth).

1. **Detect** — find every holder of the address:
   ```bash
   for h in s1 s2 s3 s4; do echo "== $h =="; ssh $h "ip -br addr | grep -E '10.0.0.(254|100)' || echo none"; done
   for h in s1 s2 s3 s4; do ssh $h 'systemctl is-active keepalived; journalctl -u keepalived -n5 --no-pager'; done
   ```
2. **Gateway VIP** is the low-risk one (routing only). Restore VRRP reachability
   between the core nodes (the partition cause); keepalived re-elects and the
   lower-priority node drops the VIP. If it is config drift, re-run
   `setup-gateway-vip.yml`. Last resort: `systemctl stop keepalived` on the wrong
   holder to force release.
3. **Storage VIP** is dangerous — it is tied to the storage **leader**, and a
   true split-brain implies two nodes think they are leader and may both try to
   map the RBD. The RBD exclusive-lock normally prevents a double *mount*, but do
   not leave it ambiguous: confirm exactly one node has `/rbd/user` mounted
   (`mountpoint -q /rbd/user`); stop services/keepalived on any extra claimant;
   let leader election settle to one holder before restarting. If both mounted
   (lock failure), treat the non-authoritative copy as suspect and unmount it
   before it diverges.

---

## Restoring authentik

authentik has **three** restore inputs; order matters:

1. **PostgreSQL** — users, flows, policies, OAuth provider/source secrets all
   live in the `authentik` DB inside `postgresql@16-main`, covered by
   `pg_dumpall`. Restore postgres first.
2. **etcd secrets** — `AUTHENTIK_SECRET_KEY`, bootstrap password/token, and the
   open-webui OIDC client secret live in etcd under
   `/cluster/config/authentik/`. They are captured by the etcd snapshot and read
   at service start. **The SECRET_KEY must be the same one** that encrypted the
   DB fields — restore etcd (or confirm those keys are present) **before**
   starting authentik, or sessions and encrypted fields break.
3. **Ansible** — blueprints, compose, `user_settings.py`, and the media branding
   under `/rbd/misc/authentik` all re-deploy via `app/install-authentik.yml`
   (that dir holds only Ansible-deployed assets, so nothing there needs a
   restore).

Then start authentik on the leader and confirm akadmin login + an OIDC round-trip
through open-webui.

---

## Ceph off-cluster backup story

**Current state (gap):** RBD pools `rbd/user` and `rbd/misc` are the *primary*
copy of their data and are **not** snapshotted or replicated off-cluster. The
database backups (pg/qdrant/etcd) are pushed offsite and cover the *stateful
services*, but non-DB files on RBD are not — concretely the docker registry
blobs (`/rbd/misc/docker-registry`, the built images; rebuildable but costly)
and anything parked on the volumes by hand outside a backed-up engine.

**Decision / recommended path:** add off-cluster RBD protection via
`rbd export-diff` to an offsite target (incremental, snapshot-based, native to
Ceph), scheduled alongside `backup-databases` on the storage leader, encrypted
with the same age recipients. `rbd snap create` → `rbd export-diff` against the
prior snapshot → `age` → rsync to destinations; prune old snapshots on the
retention window. This is heavier than the DB backups (full-volume first
export) and needs an offsite target sized for the volumes, so it is tracked as
its own item rather than bundled here. Interim mitigation: anything truly
important on RBD should also exist in a backed-up database or in Ansible.

---

## Verification drills

A backup you have never restored is a hypothesis. Two layers:

- **Nightly automated** (`backup-databases verify-restore`, on the storage
  leader, `setup-backup-verify.yml`). Loads the newest pg dump into a scratch
  PostgreSQL cluster, restores the etcd snapshot to a scratch data-dir, and
  archive-checks the qdrant snapshot, then emits the freshness/restorability
  metrics above. Catches dump corruption and broken encryption without touching
  live services. *Follow-up:* deepen the qdrant check from archive-integrity to
  a load into a temp qdrant instance. The dev-cluster system test
  (`dev/cluster/system-test.sh`, section 17) exercises this whole path —
  `backup` → `verify-restore` → success metrics — on every run.

- **Quarterly attended drill with a hardware key.** This is what proves recovery
  after loss/compromise of the storage leader (whose on-disk key the nightly
  drill depends on). An operator plugs in their `age-plugin-yubikey` token on a
  clean host, pulls the newest *offsite* backups, and restores end-to-end:
  postgres + qdrant + etcd into scratch instances, smoke-checks, then wipes.
  Record the date and result. This also exercises the operator-key custody and
  the offsite destination path, not just the on-cluster identity.
