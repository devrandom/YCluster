# cluster-heartbeat reports "healthy" on connection-refused

`cluster-heartbeat.py` reports "healthy" to healthchecks.io when the
local `/api/health` fetch errors out.
`get_cluster_health` (`cluster-heartbeat.py:52-54`) sets
`health_data['overall'] = 'error'` on the `requests` exception, but
`determine_health_status` (`cluster-heartbeat.py:148-156`) only treats
`'unhealthy'` as critical — so a connection-refused / 5xx on the local
endpoint lands in the success branch (`exit_code == 0`, POSTed to the
bare URL, healthchecks.io marks the ping OK).

Observed during the 2026-07-09 admin-api restart window: the heartbeat
log showed `Cluster Status: HEALTHY` and `Last Ping: Success` while
the `Error:`/`Cluster Error:` lines underneath both reported
`Connection refused` on `localhost:12723`.

## Fix

Add `'error'` (and a `'unknown'` / missing-fields fallback) to
`critical_conditions`, or make the local fetch a hard prerequisite —
same logic for the cluster-status block. Decide whether
`/api/cluster-status` failure should be critical (loss of cross-node
view) or warning (local still has its own data).
