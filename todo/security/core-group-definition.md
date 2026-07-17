# core group definition: naming/audit

Two sources disagree on the `core` group:

1. `inventory_boot.yml` declares `core` as the `etcd` children = s1, s2,
   s3 (the etcd quorum floor).
2. `inventory_plugins/etcd_nodes.py:268-270` adds *any* `s\d+` hostname
   to `core` at runtime, so `core` effectively becomes "all storage
   nodes" (s1, s2, s3, s4, …).

## Consequences

`setup-network-services.yml` (`hosts: core`) hits s4+ too — correct,
since they need dnsmasq with `bind-dynamic` and have no incus drop-in
conflict — but the name `core` is misleading because it conflates the
etcd-quorum floor with the broader storage set. Also
`setup-base-compute.yml` now hardcodes `bind-interfaces` for compute
nodes (s4+ are storage, not compute, so unaffected), while storage nodes
keep `bind-dynamic`.

## Options

a. **Rename the dynamic `core` group to `storage`** (or add a `storage`
   alias and use it in the playbooks).
b. **Tighten `etcd_nodes.py:269`** to match the static floor
   (`s1`–`s3` only) and add s4+ via a separate `storage` group.

## Before renaming

Audit which playbooks/variables depend on `groups['core']`.

Related: the s4-leader-eligibility item in
`../operations/s4-leader-validation.md` ("is s4 core?").
