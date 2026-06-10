# Package Management

## Held Packages

Some packages are held from automatic upgrades via `dpkg --set-selections` because
unattended-upgrades would restart their services, which are managed by leader election.
An unexpected restart of PostgreSQL (for example) causes a multi-hour outage since the
leader election script doesn't know to restart it.

Currently held:
- `postgresql-16`
- `postgresql-client-16`

The hold is applied by the `storage/install-postgres.yml` playbook.

## Upgrading Held Packages

Run the upgrade playbook manually when convenient:

```bash
cd /etc/ansible && ./run-playbook.sh storage/upgrade-held-packages.yml
```

This playbook runs `serial: 1` (one node at a time) and will:
1. Unhold packages, upgrade, re-hold
2. Restart PostgreSQL on the storage leader if it was upgraded
3. Record the upgrade timestamp for Prometheus monitoring

A `HeldPackagesStale` warning alert fires if held packages haven't been upgraded
in over 30 days.

## Checking Held Packages

```bash
# On any core node:
dpkg --get-selections | grep hold

# Check available upgrades for held packages:
apt list --upgradable 2>/dev/null | grep -E 'postgresql'
```

## Held Snaps (microceph)

Snaps are held too, and like apt holds they only stay current if someone
upgrades them deliberately. `microceph` is pinned on every storage node via
`snap refresh --hold` (applied by `storage/add-ceph-nodes.yml`) because
microceph refuses cluster operations (e.g. add OSD) when members run
different revisions — a surprise refresh on one node would wedge the
cluster, and a refresh restarts the Ceph daemons.

Upgrade with the rolling-upgrade playbook, never with ad-hoc `snap refresh`:

```bash
cd /etc/ansible && ./run-playbook.sh storage/upgrade-microceph.yml
```

It gates on `ceph health`, upgrades one node at a time (`serial: 1`, dqlite
quorum needs 2/3), does the storage leader last, waits for the cluster to
settle between nodes, and re-applies the hold afterwards.

Check for pending refreshes:

```bash
# On each storage node:
snap list | grep held          # what is pinned, and at which revision
snap refresh --list            # what a refresh would install
```
