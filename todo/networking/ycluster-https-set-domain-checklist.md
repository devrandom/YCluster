# ycluster https set-domain blast-radius checklist

Make `ycluster https set-domain` print its blast-radius checklist. The
external domain is start-time-dynamic, not watch-dynamic:
`authentik-manager` renders the OWUI/admin blueprints and
`open-webui-manager` its OIDC env from `/cluster/https/domain` at
service start, and the cert SANs come from a certbot obtain.

After a domain change the command should say:

- Restart `authentik` and `open-webui` on the leader
- Run `ycluster certbot obtain` (on the rathole node)
- Re-run `certbot-manager update-nginx`
