# ycluster wg delete: peer-only variant

`delete_peer` (`config/ansible/admin/files/ycluster/ycluster/utils/wg_config.py:183`)
removes the wg peer *and* `/cluster/nodes/by-hostname/<h>` + `by-mac/<mac>`
(symmetric with wg register, which created both), so once a node is
re-bootstrapped onto the LAN, `wg delete` silently drops its live
allocation — gone from the status page, loses its DHCP reservation
(incident: `../ycluster-private/incidents/2026-06-10 — accidental c4
allocation during canary testing`).

## Root cause underneath

A wg peer's AllowedIPs follows the node's allocation IP, so after a
wg→LAN migration the peer carries a `10.0.0.x/32` that the gateway-VIP
holder (the only node running wg0) installs as `10.0.0.x dev wg0`,
black-holing the node's return traffic into the dead tunnel and making
it unreachable from whichever node holds the VIP.

## Two fixes

1. **Give the wg layer a peer-only removal** (or have it refuse/auto-clean
   peers whose AllowedIPs fall in `10.0.0.0/24`).
2. **Make `dhcp delete` cascade to the wg peer** so one command cleans a
   migrated node.

## Operational workaround

Documented in `docs/operations/wireguard.md` (`wg revoke`, which keeps
the allocation, + a manual `ip route del <ip> dev wg0` since `syncconf`
leaves the wg-quick route behind).
