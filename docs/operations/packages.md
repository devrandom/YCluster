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
