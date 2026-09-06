# VM frontend NAT gateway

## Status

**Implemented.** The NAT gateway state machine and renderer are deployed
on the designated frontend; the WireGuard transport overlay is
provisioned by `app/install-vm-nat-wg.yml`.

Selected VMs need a public network identity that is decoupled from the
compute host they run on, for both directions:

- **Egress**: outbound traffic from the VM should appear to come from a
  frontend node's public IP, not the deployment/host IP.
- **Ingress**: external clients should be able to reach specific
  TCP/UDP port ranges on the VM, without exposing the VM's real
  address.

This is not the frp model. frp is an application-level reverse tunnel
for HTTP/HTTPS vhost routing; it does not transparently SNAT VM egress
or forward arbitrary port ranges. This design adds a separate
NAT-gateway role on the frontend.

## Goals

- Hide compute-host IP for both ingress and egress of selected VMs.
- Support per-VM ranges of TCP and UDP ports (not one tunnel per port).
- No etcd access from the frontend (it is outside the cluster envelope).
- No Ansible run required for day-to-day mapping changes.
- Default-deny: only explicitly registered VMs + port ranges are
  forwarded; VMs on the transit network cannot reach cluster-internal
  `10.*` space.

## Non-goals

- Not replacing frp for HTTP/HTTPS vhost routing.
- Not a general-purpose router between the VM transit network and the
  cluster LAN. The frontend should have no route to `10.*`.
- Not load balancing; each mapping is 1:1 frontend-port-range → VM.

## Architecture

```
                    internet
                       │
                       ▼
              frontend node (f1)
              ┌─────────────────────┐
              │ nftables NAT        │
              │  - DNAT port ranges │
              │  - SNAT egress      │
              │  - forward filter   │
              └────────┬────────────┘
                       │ per-host transit + WireGuard overlay
                       ▼
              ┌────────────────────┐
              │ compute host       │
              │  transit0 bridge   │
              │  WG spoke          │
              └────────┬──────────┘
                       ▼
                   selected VMs
```

### Transit network

Each participating compute host owns one /24 in `100.100.0.0/16`. The
address space is partitioned by host class so the numeric suffixes of
different hostname prefixes cannot collide:

| Host class | Hostname prefix | Class octet | Subnet for host index N |
|---|---|---:|---|
| GPU-passthrough compute | `nv*` | 110 | `100.100.<110+N>.0/24` |
| Other compute | `c*` | 50 | `100.100.<50+N>.0/24` |

For example, host index 3 derives `100.100.113.0/24` on an `nv*` host
and `100.100.53.0/24` on a `c*` host. These class octets deliberately
mirror the host IP allocation bases used by the DHCP allocator. An
explicit `vm_nat_transit_index`
(or a complete `vm_transit_subnet`) overrides the derivation. The
resulting third octet must be in 1–253 and unique among participating
spokes. On upgrade, the reconciler re-addresses legacy `100.100.0.x`
transit NICs and mapping records into the host's configured /24; it
frees the old reservation key and restarts a running guest after the
NIC change.

Selected VMs get a second NIC on the host's dedicated `transit0` bridge.
Each VM is pinned a static IP on this bridge by the existing `ycluster
vm` pinning logic. The VM's default route points at the bridge gateway
(`.1`), and the compute host policy-routes traffic from that /24 over
the WireGuard spoke. Exposed VMs are transit-only: when the reconciler
attaches `transit0`, it overrides the profile's `eth0` device with
`type=none` and restarts a running VM once if needed. This removes the
usable `incusbr0` route into `10.*` rather than relying on route metrics.
The Incus host's existing `incusbr0` masquerade does not apply because
`transit0` is a dedicated bridge.

The Incus ACL on `transit0` explicitly allows the local transit /24,
drops the cluster and private ranges, drops all other `100.100.0.0/16`
transit pools, and default-allows remaining egress. This preserves
internet-bound traffic while blocking cluster-local destinations; the
frontend forward chain still applies the NAT policy and final drops.

### Transport overlay

`app/install-vm-nat-wg.yml` builds a hub-and-spoke WireGuard overlay:

- The frontend is the hub at `100.101.0.1/24` on `wg-vm-nat`.
- Spoke `N` gets `100.101.0.(N+1)/24`. This control range is separate
  from the transit data range.
- The hub peer's `AllowedIPs` are that spoke's WG address and exactly
  that spoke's transit /24. It cannot learn or route a spoke's `10.*`
  space because those prefixes are never exchanged. wg-quick installs
  those exact routes on the hub.
- A spoke's peer accepts the frontend as its default path but uses
  `Table = off` plus a policy rule so only traffic sourced from its
  transit /24 is routed over the tunnel; host management traffic keeps
  the system default route. Compute spokes normally have no host
  firewall; if a forward chain exists, the overlay playbook fails and
  requires explicit transit0/WG rules there rather than bypassing it.

Run the playbook with both `frontend` and `compute` selected so spoke
keys are available when the hub configuration is rendered. Re-run
`app/install-frontend-nat.yml` afterward when upgrading an already
deployed frontend, because its environment file must select the
`wg-vm-nat` transit interface.

### Frontend NAT (nftables)

The frontend maintains an nftables table with three chains:

1. **DNAT (prerouting)** — for each mapping, rewrite
   `<public-ip>:<port-range>` to `<vm-transit-ip>:<same-range>` for the
   given protocol.
2. **Forward filter** — default drop. Private/cluster destination drops
   are installed before established-state acceptance so stale flows
   cannot survive a ruleset refresh. Allowed traffic is limited to:
   - inbound: WAN → transit VMs on registered protocol/port-range,
   - established/related return traffic after the private drops,
   - outbound: transit subnet → WAN (egress).
   No allow rule for transit → `10.0.0.0/8` or any private range.
3. **SNAT (postrouting)** — masquerade or source-NAT transit-subnet
   traffic leaving the WAN interface to the frontend's public IP.

### State distribution (no etcd on the frontend)

The frontend cannot reach etcd. Instead:

- The admin API on the storage leader gains a read-only endpoint
  (e.g. `GET /api/frontend/nat-rules`) that returns the current set of
  VM → port-range mappings. It reads from etcd server-side and exposes
  only what the frontend needs. Auth is mTLS using the same client-cert
  pattern as other cluster services.
- The frontend runs a small agent (`nat-reconciler`, systemd timer or
  daemon) that:
  1. pulls the mapping list every 30 s over the existing frp tunnel,
  2. validates it (non-overlapping ranges, known VMs, valid CIDRs),
  3. renders a complete nftables ruleset,
  4. applies it atomically with `nft -f`,
  5. stores a hash of the last-applied config so unchanged data is a
     no-op.

The endpoint is intentionally scoped: it returns only VM names, transit
IPs, protocols and port ranges — nothing else from etcd.

### Declaring a mapping

Inside the cluster, the operator (or an automation policy) writes the
intent to etcd using the existing CLI:

```
ycluster frontend expose vm-a --proto tcp --ports 20000-20099
ycluster frontend expose vm-a --proto udp --ports 20000-20099
ycluster frontend unexpose vm-a --proto tcp --ports 20000-20099
```

This stores a record under
`/cluster/frontend/nat/<proto>/<vm>/<first>-<last>` (single ports are
`<port>-<port>`). The compute-side reconciler reads this in addition to
the VM desired-state it already handles, so a VM launch/start that
references a mapping attaches the transit NIC and pins its transit IP
as part of normal reconciliation. The mapping's transit IP is pushed
into etcd by the reconciler as soon as the VM is started and the IP is
allocated.

### Reconciliation flow

1. Operator runs `ycluster frontend expose …` (or an automation does).
2. etcd updated. No Ansible.
3. Compute-side `vm-reconciler` (already runs every minute) attaches
   the transit NIC on the VM and writes the allocated transit IP into
   the mapping record.
4. Frontend `nat-reconciler` pulls the updated mapping list within its
   own tick (30 s).
5. nftables rules converge to the desired set.

Removal works the same way in reverse: delete the mapping, both
reconcilers converge, frontend rules disappear on the next tick. When
the last mapping for a local VM is deleted, the compute reconciler also
removes its `transit0` device, removes the `eth0 type=none` override so
the profile NIC is inherited again, releases the transit reservation,
and restarts a running VM if needed. If the reconciler is unavailable,
remove those two devices manually with `incus config device remove` and
let the frontend rules expire on its next pull.

## Security

- Frontend default-deny forwarding. Only registered mappings allow
  inbound WAN → transit traffic; only transit → WAN is allowed for
  egress. Transit → `10.*` is explicitly dropped.
- **No protocol bypasses the NAT path.** The forward chain's policy is
  `drop` for every IP protocol — TCP, UDP, ICMP, and anything else. A
  packet only leaves the frontend if it matches an explicit allow rule,
  and every allow rule for transit traffic lives inside the NAT
  chains (SNAT for egress, DNAT for ingress). There is no "accept all
  from transit subnet" shortcut and no separate non-NAT forwarding
  path, so ICMP, UDP, or any other protocol cannot route around the
  gateway.
- Anti-spoofing: forward rules match on input/output interface and
  source address. Traffic claiming to originate from the transit
  subnet but arriving on the WAN interface is dropped (and vice
  versa). On a spoke, a dedicated nftables forward chain accepts
  traffic entering `transit0` only when its source is that spoke's
  transit /24 and its output interface is `wg-vm-nat`; all other
  `transit0`-forwarded traffic is dropped. A guest cannot bypass the
  hub by choosing a spoofed source such as a cluster or another
  spoke's address. Hub-local input from decapsulated `wg-vm-nat`
  traffic is dropped; only WireGuard's WAN UDP encapsulation is
  reachable.
- The admin API endpoint requires mTLS; the frontend's client cert is
  scoped to this one read-only route. The frontend reaches the
  endpoint via its existing frp tunnel (plain TCP passthrough to the
  leader's nginx `127.0.0.2:443` listener). A dedicated location block
  on the public vhost (e.g. `/api/frontend/nat-rules`) is configured
  with `ssl_verify_client on` and a CA restricted to the frontend's
  client cert; other locations on the same vhost are unaffected.
- The nat-reconciler validates the full desired set before applying.
  Invalid or incomplete records (e.g. overlapping port ranges, or a
  mapping whose transit IP has not yet been allocated) are
  quarantined: they are excluded from the rendered ruleset, an alert
  is emitted, and the valid remainder is applied. This prevents a
  single stale record from freezing convergence — including deletion
  of a compromised VM's exposure. A quarantined record must be
  explicitly resolved (fixed or deleted in etcd) before it takes
  effect again.
- Rules are rendered as a complete, deterministic ruleset each pass
  (not incrementally patched), so stale state cannot linger.

### Fail-closed behavior

- The base nftables ruleset installed by Ansible at boot is
  default-deny for all transit traffic (no egress, no DNAT). A freshly
  provisioned or rebooted frontend passes no VM traffic until the
  reconciler's first successful apply.
- If the pull endpoint is unreachable or the returned data fails
  validation, the reconciler keeps the last known-good ruleset in
  place. Existing mappings keep working; no new or changed mapping is
  applied.
- If no last-known-good ruleset exists (e.g. first apply fails), the
  base deny-all ruleset remains active — no transit traffic flows.
- The reconciler never falls back to "no rules"; an empty or invalid
  desired set is always rendered as an explicit deny-all ruleset, not
  as a flush.

## Open questions

- Mapping records use `/cluster/frontend/nat/<proto>/<vm>/<first>-<last>`.
  Reservation records use `_ip/` keys; non-legacy pools are namespaced by
  their /24 to prevent duplicate octets across spokes.
- Whether the frontend needs a separate "egress-only" mode (SNAT but
  no DNAT) for VMs that should not be reachable inbound.
- Whether UDP and TCP ranges should always be paired in the CLI or
  independent.
