# Single source of truth for public vhost configs

There used to be two parallel, hand-maintained config paths that decided
which public vhosts exist, and nothing kept them in sync:

1. **Per-app templates** auto-discovered by `certbot-manager update-nginx`
   in `/etc/nginx/templates/*_<name>.conf.j2` and rendered to the rathole
   endpoint `127.0.0.2:443`. Adding a template automatically exposed the
   service on that path.
2. **frp aggregator** `files/admin-api-alt.conf.j2` whose `server_name`
   blocks on `127.0.0.2:17443` were enumerated by hand (`admin.`, `app.`,
   `auth.`, open-webui, …). Nothing discovered new vhosts and added them
   here.

## Observed failure

`comfy` was added only to the per-app template path, so it worked on the
rathole endpoint but silently 404'd on the live public path (frp →
`127.0.0.2:17443` → `server_name _` fallback `return 404`), because no
matching block existed in the aggregator.

## Fix (implemented — single source)

Collapse to a single source of truth, now that rathole has been ripped out
(see `switch-rathole-to-frp.md`) and the internal `:443` mirrors the
external surface:

- Each per-app vhost template (`open-webui.conf.j2`, `ycluster_app.conf.j2`,
  `authentik_auth.conf.j2`, `comfyui_comfy.conf.j2`) declares **both**
  listeners in one `server` block: the plain internal/direct `:443`
  (`:80` in `--local` dev mode) **and** the frp landing
  `127.0.0.2:17443 ssl http2 proxy_protocol` (with `set_real_ip_from
  127.0.0.1; real_ip_header proxy_protocol;`).
- `admin-api-alt.conf.j2` keeps only the frp proxy-port surface that is
  *not* handled by a per-app template:
  - the HTTP `:17080` default (ACME HTTP-01 + admin public/authed
    includes),
  - the `_` HTTPS 404 fallback on `127.0.0.2:17443`,
  - the public `admin.<domain>` block on `127.0.0.2:17443`.
  **Admin is the one exception**: the internal admin vhost
  (`admin-api.conf.j2` on plain `:443`) is a deliberately identity-less
  operator surface, so the forward-authed public admin surface stays a
  distinct block here.
- "Add a vhost" now edits one per-app template and both the internal
  `:443` and the frp `127.0.0.2:17443` paths work.

Steps:

1. Validate nginx accepts per-listen `proxy_protocol` on one listener
   while the sibling listener on the same block stays non-PROXY. ✅
2. Update per-app templates (open-webui, ycluster_app, auth, comfy) to
   drop the dead rathole `127.0.0.2:443` listener and carry the PROXY
   landing `127.0.0.2:17443` in the same block as the internal `:443`. ✅
3. Trim `admin-api-alt.conf.j2` down to the `:17080` HTTP default, the
   `_` 17443 fallback, and the public `admin.<domain>` block. ✅
4. Update the install plays so the alt surface is derived, not
   hand-listed (remove the dead comfyui-locations copy from
   `install-frp-alt-vhost.yml`; `install-comfyui.yml` already owns it). ✅
5. Redeploy and verify each `name.<domain>` on `127.0.0.2:17443` and on
   internal `:443` return the same 302-to-authentik / proxied response.

## Safety net (future — deploy-time parity guard)

Keep the derived surface honest by having
`certbot_manager.update_nginx_configs` fail loudly if any discovered
vhost's `server_name` is missing from the `127.0.0.2:17443` surface —
turns the silent 404 into a hard deploy error. (Not yet implemented; the
single-source templates already prevent the drift class of bug.)
