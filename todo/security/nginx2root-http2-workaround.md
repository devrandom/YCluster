# Undo the nginx2root HTTP/2 workaround once nginx is patched

Xint disclosed the "nginx2root" set on 2026-11-18. Two of the five CVEs need
HTTP/2 traffic and one of them (CVE-2026-123007) has been present since nginx
1.9.5, so every nginx we run is affected: the vhosts on all four storage nodes
listen `443 ssl http2` (cluster network) and `127.0.0.2:17443 ssl http2
proxy_protocol` (the frp landing that the frontend relays public traffic to).
Fixes exist upstream in 1.30.7 stable / 1.31.9 mainline; noble-security still
carries `1.24.0-2ubuntu7.19` with no backport, and 1.24 has no `http2 on;`
directive, so HTTP/2 can only be turned off by dropping the protocol option
from each `listen` line.

## What is deployed as the workaround

- `config/ansible/mitigate-nginx2root.yml`, imported at the end of
  `site.yml`. On `storage` it strips `http2` from the deployed templates in
  `/etc/nginx/templates`, re-renders them with `certbot-manager update-nginx`,
  asserts no HTTP/2 listener is left, and reloads nginx.
- The vhost templates in `config/ansible/admin/files/` and
  `config/ansible/app/files/` no longer ask for HTTP/2, so re-running an app
  playbook cannot put it back.
- `/etc/modprobe.d/99-disable-sctp.conf` (`install sctp /bin/false`) closes
  the kernel SCTP privilege-escalation half of the chain on the storage nodes
  only.

Cost while it is in place: every public vhost falls back to HTTP/1.1 (no gRPC
is served, so nothing breaks; multiplexing is lost), and SCTP cannot be loaded
on the storage nodes. Restoring `http2` is not safe until the running nginx
*binary* has the fix — a reload keeps the old binary, so the upgrade needs a
service restart.

## Plan

1. Track the upgrade through osv-feed (it already tracks the `nginx` package):
   wait for a build that is installed *and* named in the advisory, i.e.
   upstream 1.30.7/1.31.9 or an Ubuntu `1.24.0-2ubuntu7.20`+ that references
   CVE-2026-123007.
2. Upgrade and restart nginx on the storage nodes, leader last, verifying the
   public path (`https://<domain>/` via the frontend) after each node.
3. Roll the workaround back: restore `http2` in the eleven vhost templates,
   remove the `site.yml` import and `mitigate-nginx2root.yml`, delete
   `/etc/modprobe.d/99-disable-sctp.conf` from the storage nodes, then reload
   nginx and confirm ALPN negotiates `h2` again.
4. Decide separately whether the kernel SCTP fix lands before that rollback;
   if it does, the drop-in can go first. Widening the SCTP block to the other
   node classes is not worth doing — the kernel fix retires it everywhere.

## Acceptance

- `nginx -v` on every storage node reports a build carrying the fix, and the
  service was restarted (not reloaded) since the upgrade.
- `openssl s_client -alpn h2` against `127.0.0.2:443` on the leader selects
  `h2` again, and a public request through the frontend shows `HTTP/2` in
  `curl -w '%{http_version}'`.
- `mitigate-nginx2root.yml` and `/etc/modprobe.d/99-disable-sctp.conf` are
  gone, and a full `site.yml` run reports no change to the vhosts.
