# Storage-leader election: renew lease during startup

Observed in production. `start_all_services()` in
`config/ansible/storage/scripts/storage-leader-election` blocks on
`systemctl start user-rbd` while holding leadership, but does not renew
the etcd lease. With the 30s lease TTL, a slow RBD acquire (retries,
Ceph timeouts) can outlive the lease and let a second node acquire
leadership while the first node still holds/holds-then-releases the RBD
exclusive lock. On s3/s1 this produced two leaders within ~30s; RBD
exclusive lock prevented double-mount, but s3 left a failed
`user-rbd.service` and both nodes then went through teardown. The
`user-rbd-manager` stale-lock breaker then tried `rbd lock remove` with
the locker as ID and cookie `null`, which is both a parsing bug and an
unsafe design.

## Fix shape

- Run lease renewal concurrently with `start_all_services()` (or make
  startup asynchronous with dependencies), so a 30s+ RBD acquire cannot
  forfeit leadership.
- Before attempting `rbd map --exclusive`, confirm we still hold
  `/cluster/leader/app`; abort cleanly if not.
- Bound the `rbd map` attempt with a timeout so a hung Ceph acquire
  cannot wedge startup indefinitely. Do NOT adopt a blanket
  "abort within one lease TTL" policy: converting recoverable Ceph
  slowness into leader teardown (xfs shutdown, pkill, force unmount)
  trades a rare race for leader flapping. Bounded, not aggressive.
- Remove or tightly fence the stale-lock-breaking logic in
  `user-rbd-manager`; never break RBD locks based on retry count alone.
- Fix the lock-list parsing (`id`/`locker` vs `cookie`) if any manual
  break tooling is kept.

## Test

Dev cluster failover with an injected delay in the user-rbd start path
(or a stand-in unit that sleeps) should not yield two leaders.
