# Consolidate redundant etcd reads in admin-api health path

On a core node, every `/metrics` scrape (frequent, per-node by
Prometheus) makes ~6 separate etcd round-trips inside
`get_comprehensive_health`: the `/test` probe, `is_storage_leader`
(`/cluster/leader/app`), `is_dhcp_leader` (`/cluster/leader/dhcp`),
`is_node_drained` (`/cluster/nodes/<h>/drain`), `check_certificate_expiry`
(`/cluster/tls/cert`), and `get_core_nodes` → `get_all_hosts` (a full
`by-hostname/` *prefix scan*).

Fetch them in one batched/cached pass (e.g. a short TTL cache or a
single multi-key read per scrape).

Non-core nodes already make zero etcd calls here after the
etcd-access-hardening Phase 1 gating; this is the core-node follow-up.
