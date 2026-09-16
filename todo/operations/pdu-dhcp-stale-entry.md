# Auto-clean stale DHCP allocations before PDU setup

When a new PDU boots with DHCP, it may get auto-registered under `dhcp-NNN`
with `type: compute`. The `setup-pdu-ip.yml` playbook matches by MAC across
all `by-hostname/` entries, so the stale `dhcp-NNN` entry blocks PDU slot
allocation — the playbook reports "already configured" instead of assigning
a `pdu{N}` hostname.

Currently this requires a manual etcd delete of the stale entry before
running the playbook. The playbook should handle this itself: when it finds
a MAC match whose hostname is a dynamic `dhcp-NNN` allocation (not a real
`pdu{N}` slot), it should delete that entry and proceed with slot
allocation.

## Broader problem: dhcp-NNN records are permanent

The playbook-specific fix above is the narrow version. The underlying issue
is that `dhcp-NNN` etcd records are written once and never cleaned up:

- `/api/dhcp-config` generates dnsmasq entries with `infinite` lease from
  these records, so the MAC→IP binding is effectively permanent once
  created.
- The DHCP lease itself expires, but the etcd record does not — it's a
  static reservation, not a lease.
- The only automatic cleanup is `_retire_old_hostname()` in
  `dhcp_allocation.py`, which fires only when the same MAC is re-allocated
  through the normal flow (e.g., gets a proper hostname). There is no TTL,
  no reaper, no periodic sweep.
- Any unknown device that ever touches DHCP gets a permanent record
  squatting on the dynamic IP range, even after the device is
  decommissioned or re-registered as something else.

A periodic reconciliation (compare `dhcp-NNN` etcd records against active
DHCP leases / ARP entries, delete genuinely stale ones) would address the
general case.
