# WG inbound ingress follows the active leader

WG inbound ingress needs a stable externally-routable endpoint that
follows the active leader. Currently the DNAT on the upstream router
points at a specific core node's uplink IP (e.g. `192.168.0.104` = s3),
so wg breaks on gateway-VIP failover to s1/s2.

## Options

1. **Uplink-side VIP** via keepalived on the `192.168.0.x` segment — clean
   but only s2/s3 are on that subnet (s1 is on `192.168.9.x`), so s1 can't
   participate and gateway-VIP leadership would need to be constrained to
   s2/s3.
2. **Static route** `10.0.0.0/24 via <core node>` on the router so DNAT can
   target `10.0.0.254` directly — simplest but single-next-hop fragility.
3. **Tiny UDP forwarder** on each core node that knows which peer currently
   holds the gateway VIP and proxies wg packets there.

Option 1 is probably the right call once s1's uplink is reconciled.

## Beyond follow-the-leader: redundant ingress

The stronger goal is **redundant** inbound paths — multiple ingress IPs
live at once (e.g. both core uplinks + the rathole endpoint), not just
one address that moves on failover, so a single dead uplink/route doesn't
strand remote peers (incident:
`../ycluster-private/incidents/2026-07-02 — NodeDown flap from
157.131.52.160 uplink loss`).

WireGuard has **no native support** for this: a peer config carries a
single `Endpoint`, with no endpoint list or failover (it's roaming-aware
for the *client's* source address, and re-resolves a DNS-name Endpoint on
`wg-quick up`, but never health-checks or fails over between ingress IPs
on a live tunnel). So redundancy has to be built around wg:

1. Low-TTL DNS for the Endpoint + client re-resolution (not automatic
   mid-tunnel).
2. **Client-side watchdog** that flips the peer endpoint
   (`wg set wg0 peer <pub> endpoint <alt>`) when
   `wg show latest-handshakes` goes stale — closest to true redundancy,
   and the smarts live on the remote nodes we control.
3. Anycast / a front VIP / the UDP-forwarder above presenting one stable
   address over several paths.

(b) composes well with the follow-the-leader work: the server side keeps
moving wg0 with the VIP while clients independently pick whichever ingress
is currently answering.

## Note

The rathole-PROXY-protocol half of the original "WG inbound ingress +
rathole PROXY protocol" bullet is folded into
`switch-rathole-to-frp.md` — frp has native PROXY support, so
the migration picks up that fix for free.
