# admin-api: cached etcd client failover

`get_etcd_client()` (`config/ansible/admin/files/ycluster/ycluster/common/etcd_utils.py:86`)
returns a process-wide `_CACHED_CLIENT`, and `connect_with_retry` pins it
to the *first reachable* host in `ETCD_HOSTS` (`10.0.0.11,12,13`). The
python etcd3 client doesn't round-robin and the cache never reconnects,
so draining whichever etcd node is first in the list blinds every
long-running `_CACHED_CLIENT` consumer until restart — and because the
read errors are swallowed into empty dicts, the health surface
(`/api/cluster-status`, `ycluster health`) goes blank with a *fresh*
timestamp, looking exactly like a stuck failover even when storage has
failed over cleanly.

## Fix shape

On `ConnectionFailedError`/`_InactiveRpcError`, drop and rebuild
`_CACHED_CLIENT` against the remaining `ETCD_HOSTS` (or hand the whole
list to a failover-capable client) so reads survive losing any single
etcd node.

## Same root cause as

`../code-quality/python-etcd-cached-client.md` (the code-review version of
this — keep this entry focused on the admin-api operational impact).
