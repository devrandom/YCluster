# NTP / Chrony

## Why no `prefer` on the local NTP server

`config/ansible/templates/chrony.conf.j2` deliberately does NOT use
`prefer` on the line pointing at the gateway VIP (10.0.0.254).

The VIP is held by one storage node at a time via keepalived, and
every node — including the VIP-holder — points at it. With `prefer`,
chronyd on the VIP-holder selects itself as the reference, which is a
feedback loop. chronyd then never updates its Reference Timestamp from
upstream (observed stuck at the install date after 35+ days), the
system clock drifts ~1.4 s behind true UTC, and LAN-attached nodes
follow it. US/WireGuard nodes reject the offset and stay correct,
producing the visible LAN/US skew.

Without `prefer`, chrony's normal selection picks the best source by
combined stratum + offset + dispersion score, which is always an
external one (`time.google.com`, `pool.ntp.org`).

## Recognizing the symptom

On any node, compare `chronyc tracking`'s `Ref time` to the current
date. If `Ref time (UTC)` is more than a few hours behind, the local
NTP server is stuck. Cross-check with an external source:

```bash
sntp -S time.google.com
```

A skew of ~1 s or more confirms it. The smoking gun on the VIP-holder
is a `Ref time` from days ago and `Stratum` ≥ 5 even though external
sources are reachable.

## Recovering

If `proxy.xc` (10.0.0.254) is stuck:

```bash
sudo systemctl restart chrony   # on the VIP-holder
```

Watch `chronyc tracking` on a LAN node until `Ref time` advances and
external sources become the selected reference (`^*`).

macOS nodes use `systemsetup` (no chrony) and only slew by default —
after a large proxy.xc offset fix, they may need a one-shot step:

```bash
sudo sntp -sS 10.0.0.254
```

m3-style nodes (admin user, locked-down root SSH) need the `sudo`
form; m1/m2 (root SSH allowed) can run it directly.
