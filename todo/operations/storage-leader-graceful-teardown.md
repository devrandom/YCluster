# Storage-leader election: graceful teardown

Found via the dev cluster. `stop_all_services` in
`config/ansible/storage/scripts/storage-leader-election` stops
`ycluster-apps.target`/`docker-registry`/`rathole`/`misc-rbd`/`user-rbd`
**in parallel** (5s-timeout background jobs) and never gracefully stops
`postgresql@16-main`/`qdrant` at all — so the rbd units' plain `umount`
always loses the race against the services still holding the mount
(EBUSY, swallowed by `|| true`), the `pgrep postgres` check then always
finds survivors, and **EVERY failover takes the aggressive path**:
`pkill -9 postgres` + `xfs_io shutdown` + `umount -f -l` + force unmap.
Postgres gets a crash-stop (WAL recovery on the next leader) on every
routine drain.

## Fix shape

Stop the mount *consumers* first and wait (`postgresql@16-main`, `qdrant`,
`ycluster-apps.target`, `docker-registry`, `rathole` — bounded timeout),
then stop `misc-rbd`/`user-rbd` so their umounts see a free mount; keep
the aggressive path as the fallback it was meant to be.

## Evidence from dev

The parallel stop left a stale `/rbd/user` mount on the drained node
(containers can't `umount -f -l`), which broke every `mountpoint -q`
leader detection until swept — in prod the force-unmap hides the same
race. The dev stand-in units work around it with an ExecStop umount
retry loop (`dev/cluster/app-dev.yml`); fixing the election script's
stop ordering would let prod failovers hand over a cleanly-shut-down
postgres and make the dev workaround unnecessary.

## Test

On the dev cluster (`system-test.sh` section 16 exercises exactly this
path).
