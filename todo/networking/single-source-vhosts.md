# Single source of truth for public vhost configs

There are two parallel, hand-maintained config paths that decide which
public vhosts exist, and nothing keeps them in sync:

1. **Per-app templates** auto-discovered by `certbot-manager update-nginx`
   in `/etc/nginx/templates/*_<name>.conf.j2` and rendered to the rathole
   endpoint `127.0.0.2:443`. Adding a template automatically exposes the
   service on this path.
2. **frp aggregator** `files/admin-api-alt.conf.j2` whose `server_name`
   blocks on `127.0.0.2:17443` are enumerated by hand (`admin.`, `app.`,
   `auth.`, open-webui, …). Nothing discovers new vhosts and adds them here.

## Observed failure

`comfy` was added only to the per-app template path, so it worked on the
rathole endpoint but silently 404'd on the live public path (frp →
`127.0.0.2:17443` → `server_name _` fallback `return 404`), because no
matching block existed in the aggregator.

## Fix (option A — structural)

Collapse to a single source of truth:

- Each per-app vhost template declares **both** listeners in one `server`
  block: the rathole `listen 127.0.0.2:443 ssl http2` **and** the frp
  `listen 127.0.0.2:17443 ssl http2 proxy_protocol` (+ `set_real_ip_from
  127.0.0.1; real_ip_header proxy_protocol;`).
- Remove the per-hostname blocks from `admin-api-alt.conf.j2`, leaving
  only the `_` 404 fallback.
- "Add a vhost" then edits one template and both tunnels work.

Steps:

1. Validate nginx accepts per-listen `proxy_protocol` on one listener
   while the sibling listener on the same block stays non-PROXY.
2. Update per-app templates (open-webui, ycluster_app, admin-api, auth,
   comfy) to add the `127.0.0.2:17443 proxy_protocol` listener.
3. Trim `admin-api-alt.conf.j2` down to the `_` fallback.
4. Update both install plays (`setup-web-services.yml` /
   `install-frp-alt-vhost.yml`) so the alt surface is derived, not hand-listed.
5. Redeploy and verify each `name.<domain>` on `127.0.0.2:17443` returns
   the same 302-to-authentik / proxied response as on `127.0.0.2:443`.

## Safety net (option B — deploy-time parity guard)

Keep the aggregator (or the derived alt surface) honest by having
`certbot_manager.update_nginx_configs` fail loudly if any discovered
vhost's `server_name` is missing from the 17443 surface — turns the
silent 404 into a hard deploy error.
