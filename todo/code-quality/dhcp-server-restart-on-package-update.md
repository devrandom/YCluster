# dhcp-server restart on package update

`install-ycluster-package.yml` restarts admin-api when the package
updates but not dhcp-server, so the DHCP leader keeps running old
package code until something else restarts it (seen in production; the
deployment incident log has the detail).

## Fix

Add dhcp-server (when active) to the package-update restart, like the
etcd-endpoint-change handler already does.
