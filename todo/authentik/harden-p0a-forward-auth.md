# Harden P0-A gating: real authentik forward-auth at the edge

P0-A components are tiered as "behind a credential gate," but the gate is
inconsistent. ComfyUI and the admin-api/ycluster_app surface are genuinely
gated at the nginx edge by the authentik embedded-outpost forward-auth
(`auth_request /outpost.goauthentik.io/auth/nginx`, e.g.
`admin/files/comfyui-locations.conf`). Open WebUI and Zulip are **not**: in
`admin/files/open-webui-locations.conf` and `app/files/zulip-locations.conf`
the `location /` block proxies straight to the backend with no forward-auth.
Their only gate today is app-level — `ENABLE_SIGNUP=False` /
`ENABLE_LOGIN_FORM=False` + OIDC for OWUI, `zulip_auth_backends:
SAMLAuthBackend` for Zulip — so the app itself still receives raw pre-auth
bytes across the whole `/` surface. This contradicts the strict reading of the
P0-A rule in `docs/design/perimeter-software-tracking.md` (a gate should keep
untrusted pre-auth bytes off the component).

## Goal

Move every P0-A web component behind the same edge gate: nginx forward-auth to
the authentik embedded outpost *at the vhost*, so `location /` returns
`auth_request` results before any bytes reach the app.

## Scope (P0-A items from `config/ansible/perimeter_software.yml`)

- Open WebUI (`open-webui-locations.conf` `location /`) — add the
  outpost `auth_request` like comfyui; keep `/healthz` and the `/v1/`
  inference path working (the `/v1/` API-key gate on `:4002` may stay for
  non-interactive clients, but interactive `/` must be edge-gated).
- Zulip (`zulip-locations.conf` `location /`) — same forward-auth; verify
  Zulip's websockets/long-polling survive the gate.
- ComfyUI / admin-api — already gated; confirm the pattern is shared and no
  P0-A app is left relying on a disabled-login flag alone.
- frp control/tunnel token, per-core SSH tunnels, VM bastion — key/token
  gated, not nginx; N/A for this edge work, but worth a pass that their keys
  can't be bypassed.

## Considerations / risks

- App-level login (OWUI/Zulip's own auth tables) becomes redundant with the
  edge gate; decide whether to keep or disable it after the edge gate lands
  (see `todo/authentik/split-passwords-frontend.md` and the OWUI
  `ENABLE_LOGIN_FORM=false` convergence note).
- Changes touch certbot-manager-rendered vhosts; re-render and `nginx -t`
  before reload, verify external HTTPS through the public path after.
- OWUI's `/healthz` and inference probes must stay reachable (exempt from the
  gate); keep an internal path for local-ai-proxy.
- Do not degrade the P0-A *detect/deploy* SLA while this is in flight.

## Definition of done

Every P0-A web component listed above is behind `auth_request
/outpost.goauthentik.io/auth/nginx` at the vhost, the inventory comments and
design doc describe the edge gate (not just disabled local login) as the
boundary, and the change is verified end-to-end through the public path.
