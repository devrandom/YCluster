# Incus DNS host-record system test on the dev cluster

Turn the incus DNS host-record verification into an automated system test
on the dev cluster. The 2026-06-10 fix was verified by hand against
`dev/cluster/` (the dev nodes are pinned, never-DHCP containers — the
exact lease-less case the fix targets); script it as a repeatable test,
e.g. `dev/cluster/test-dns-sync.sh`:

1. `dev-cluster.sh up`
2. Baseline — clear `host-record=` lines from `ycdev0` `raw.dnsmasq`,
   assert `dig @10.0.0.1 s1.incus` is NXDOMAIN
3. Run `sync_dns_records()` (venv python, package from
   `config/ansible/admin/files/ycluster`), assert all pinned nodes
   resolve and a second run returns `{}`
4. Lifecycle — `incus init` a throwaway pinned instance, sync, assert
   it resolves; delete it, sync, assert NXDOMAIN with others intact
5. Ownership split — run the `install-incus.yml` resolver-task shell
   against `ycdev0`, assert host-records survive and the task is
   idempotent, then assert `sync_dns_records()` preserves the resolver
   lines; restore `raw.dnsmasq` at the end

This would be the first scripted system test on the dev cluster —
structure it so further tests (etcd TLS migration rehearsal, leader
election) can join a common harness (`dev/cluster/tests/`).
