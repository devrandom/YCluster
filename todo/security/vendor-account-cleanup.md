# Vendor account cleanup

Clean up stale/vendor-created user accounts on managed nodes (incident:
`../ycluster-private/incidents/2026-06-09 — exx vendor account on nv2`).
Vendor factory-preinstall accounts can ship with full sudo, `lxd`
membership (root-equivalent), lingering login sessions, a populated
`authorized_keys` (potentially holding a cluster key), and a squatted
UID 1000 (forcing `dev` to a non-standard UID) — and a fresh vendor
image comes up unmitigated.

## Cleanup per account

- Verify nothing references it (cron, services, file ownership under its
  home)
- Kill its sessions
- `deluser --remove-home`
- Decide whether to renumber `dev` back to UID 1000 (chown sweep) or
  leave it

## Sweep the non-PXE node types

nvidia, nas, macos — fold the check into their bootstrap playbooks.
