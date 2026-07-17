# Per-node uplink failover watchdog

A core node whose direct uplink's WAN is dead but whose next-hop is
still ARP-reachable keeps its metric-100 DHCP default route and
blackholes egress instead of falling back to the gateway VIP (`.254`,
metric 500) — the kernel only drops a route on carrier/next-hop loss,
not on dead-WAN-behind-a-live-router, so the leader can lose internet
(and its rathole tunnel) while still looking up (incident:
`../ycluster-private/incidents/2026-06-17 — cold-start uplink-failover
blackhole`).

## Fix

A small per-node systemd-timer watchdog that probes the internet
*forced through the own uplink* (reuse `gateway_health.py`'s
`SO_BINDTODEVICE` probe so it still detects recovery after failover)
and, with hysteresis, bumps the own-uplink default route's metric above
the `.254` fallback when dead / restores metric 100 when healthy.

Composes with the gateway VIP, which already lands on a healthy-uplink
node (so the `.254` a demoted node leans on is guaranteed to NAT out a
working uplink); the VIP holder's uplink is healthy by definition, so
the same one rule needs no special-casing.

## The route is DHCP-owned by systemd-networkd

(`proto dhcp`, metric 100, rendered from `netplan-cluster.yaml.j2`), and
networkd re-asserts it on every lease renew (T1 ≈ 54 min on a 2 h
lease), on any reconfigure, and on reboot — so a manual
`ip route del`/metric change loses to networkd.

The watchdog must therefore **NOT fight networkd's main-table route**;
use one of:

1. **Watchdog-OWNED lower-metric default via `.254`** toggled on health
   (e.g. `metric 50` when the uplink is dead so it beats the DHCP
   `metric 100`, withdrawn when healthy — survives renews but not
   reboot).
2. **Policy routing** (`ip rule` selecting an uplink table vs a gateway
   table), independent of the networkd-managed route.

Add a note to the cold-start runbook (`docs/operations/disaster-recovery.md`).
