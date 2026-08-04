# Public HTTP/HTTPS routing

How a `name.<domain>` service gets from an external browser to a cluster
backend, and how to add a new public vhost. This is the canonical picture;
use it before probing "why is my vhost 404/500?" so you check the right
node.

## The path, end to end

```
external user
   https://name.<domain>                  (public DNS -> frontend public IP)
        |
        v
frontend node (fN)  frp/rathole server  (public 80 / 14443, alt 17080/17443)
        |  TCP tunnel (raw bytes: SNI + HTTP Host are preserved)
        v
STORAGE LEADER      127.0.0.2:80  OR  127.0.0.2:443   <-- the actual endpoint
        |  nginx server_name match on Host/SNI
        v
storage-leader nginx vhost  (e.g. comfy.<domain> / app.<domain> / auth.<domain>)
        |  authentik embedded-outpost forward-auth (outpost.goauthentik.io)
        v
backed service  (e.g. comfy backend on <gpu-host>.xc:8188, open-webui, app server)
```

Key facts that end up mattering in practice:

- **External HTTP/HTTPS lands on the storage leader's `127.0.0.2:80` /
  `127.0.0.2:443`.** The frp/rathole client on the leader forwards the tunnel
  to that loopback address. `127.0.0.1` is reserved for cluster-internal
  health checks and full admin-api access; `127.0.0.2` is the public-facing
  nginx listener.
- **Only the storage leader serves public vhosts.** Non-leader storage nodes
  run their own nginx but receive no external tunnel traffic, so a vhost
  there is inert (and a local probe can return 500 when its auth subrequest
  can't reach the leader's outpost). Verify on the leader, not on siblings.
- **Don't hairpin-test.** Curling the frontend/public IP (or a core node's
  internal address) from inside the cluster does not traverse the tunnel and
  will reliably 404 on the `_` default vhost even when the real path works.
  Test against the leader's `127.0.0.2:443` directly, or through an external
  client.
- The tunnel is raw TCP with no PROXY protocol on the primary rathole path
  (frp alt ports add it — see `docs/design/frp-migration.md`). SNI and the
  HTTP `Host` header pass through untouched, so nginx routes purely by
  `server_name`.

## Anatomy of a public vhost

Vhosts are rendered by **certbot-manager** from nginx templates:

- Template source: `files/<app>/<app>_<name>.conf.j2` (pattern:
  `..._<name>.conf.j2`). The `_<name>` suffix sets
  `server_name = name.<domain>` and adds it to the certificate SAN.
- `certbot-manager update-nginx` renders the site to
  `/etc/nginx/sites-available/<site>` and it must be symlinked into
  `/etc/nginx/sites-enabled/` before nginx includes it.
- Shared IdP edge plumbing lives in the `admin-api-authentik-outpost.conf`
  include (login redirect, OAuth callback, `auth_request`).
- The vhost listens on `127.0.0.2:443` (TLS) with the shared
  `/etc/nginx/ssl/cert.pem`. `name.<domain>` in the cert SAN lets the
  frontend/LB terminate correctly.

See `config/ansible/app/install-comfyui.yml` end of file for a worked
example (resolves a backend URL from host_vars, installs template, renders
with certbot-manager, symlinks into sites-enabled, reloads nginx).

## Adding a new public vhost — checklist

1. Add a `files/<app>/<app>_<name>.conf.j2` template (mirror
   `comfyui_comfy.conf.j2`: `listen 127.0.0.2:443`, `server_name`
   from the `_<name>` suffix, outpost include, `proxy_pass` to the backend).
2. Render/install/enable/reload it from a `hosts: storage` play so it lands
   on the storage leader (same shape as the comfy vhost tasks).
3. Add the IdP side: an application + forward provider + policy bindings to
   a group in `authentik-manager` (the `comfy` group / `ycluster-admins`
   pattern), then run `install-authentik.yml`.
4. Deploy via the `deploy-to-cluster` workflow, then verify on the **storage
   leader's `127.0.0.2:443`**:
   ```bash
   curl -sk --noproxy '*' --resolve name.<domain>:443:127.0.0.2 \
        https://name.<domain>/
   # unauthenticated -> 302 to /outpost.goauthentik.io/start (expected)
   ```
5. DNS: `name.<domain>` must resolve to the frontend public IP. No per-vhost
   DNS is set up by ansible; add the record out of band.
