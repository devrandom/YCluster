# dhcp-server restart on package update

`install-ycluster-package.yml` restarts admin-api when the package
updates but not dhcp-server, so the DHCP leader keeps running old
package code until something else restarts it (incident:
`../ycluster-private/incidents/2026-06-10 — dhcp-server ran stale code
after B3 rollout`).

## Fix

Add dhcp-server (when active) to the package-update restart, like the
etcd-endpoint-change handler already does.
