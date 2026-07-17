# ycluster CLI: auto-load etcd-client.env

`ycluster` CLI: `get_etcd_hosts()` (`common/etcd_utils.py:12`) falls
back to `localhost:2379` when `ETCD_HOSTS`/`ETCD_HOST` are unset, so
any caller that forgets to `set -a; . /etc/ycluster/etcd-client.env`
first silently hits localhost and fails with "Could not connect to any
etcd host".

The env file is the cluster's single source of truth for etcd
endpoints + mTLS certs; the CLI should auto-load it (or read it
directly) when present, so callers don't have to remember the source
dance. Same fix removes the per-script `set -a; .
/etc/ycluster/etcd-client.env` boilerplate from `backup-databases`,
`storage-leader-election`, and now `setup-backup-verify.yml`'s
recipients tasks.
