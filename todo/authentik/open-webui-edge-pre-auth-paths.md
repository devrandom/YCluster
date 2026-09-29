# Open WebUI sits entirely behind the authentik edge outpost — carve out its pre-auth surface

Open WebUI (`chat.<domain>`) is served behind the authentik embedded outpost
via nginx `auth_request /outpost.goauthentik.io/auth/nginx` as the edge gate
for **all** of its traffic. That is almost certainly too coarse: Open WebUI
has paths that are meant to run *before* / *outside* the IdP auth flow (its
own OIDC/SSO handshake, static assets, metadata, health probes), and sending
those back through authentik can bounce or loop the SSO return, especially
when the outpost session has expired or the outpost throws a 5xx. This is a
plausible cause of the recurring daily logouts and intermittent 500s users
hit.

Observed: a user hit a 500 on `chat.<domain>`. Note this may **not** be
logged as a 500 — nginx's `error_page 500 502 503 504 /50x.html` on the gated
`location /` can translate an outpost/auth-subrequest failure into the
friendly 50x page with HTTP 500 on the fly, so the real cause is upstream of /
inside the auth gate.

**Per repo rules: do not touch prod security/stability and no reboots.** The
nginx locations change below is applied to the cluster via the standard
playbook path; the ops/deploy record is in the gitignored `NOTES.md`, not
this public file.

## Current topology

- Edge provider `open-webui-edge` uses
  `access_token_validity: days=1`
  (`config/ansible/app/files/authentik-manager`).
- Open WebUI's own session JWT defaults to `JWT_EXPIRES_IN=4w`
  (`config/ansible/app/files/authentik-manager`).
- nginx `location /` for `chat.<domain>` runs
  `auth_request /outpost.goauthentik.io/auth/nginx` before any bytes reach
  the app (`config/ansible/admin/files/open-webui-locations.conf`,
  `config/ansible/app/files/open-webui.conf.j2`).
- The locations file is deployed as a template by
  `config/ansible/admin/setup-web-services.yml` to
  `/etc/nginx/open-webui-locations.conf`; `(( port ))` → `8380`.

## Findings

1. **Daily kicks.** The outpost proxy session expires every 24h
   (`access_token_validity: days=1`) while OWUI's own JWT stays valid for 4
   weeks — so users get bounced by the edge gate long before their OWUI
   session is actually stale. This matches the "kicked out daily" reports.
2. **Refresh-token failure.** Open WebUI logs on the storage leader show:
   - `No refresh token available for session ...`
   - `Failed to refresh token ... deleting session`
   (provider `oidc`, user `9f841097-1526-44d6-becd-e353fa57c83e`).
   Authentik is not issuing a refresh token, so when the 1-day access token
   expires OWUI can't refresh and deletes its OAuth session.
3. **500 masking.** The friendly-50x `error_page` on the gated `location /`
   can convert an outpost/auth-subrequest failure into a 500 on the response,
   consistent with the report that the 500 may not appear as a 500 in the
   access log.

Read-only log inspection at the time found no matching 500 in that day's
nginx access log (only scanner noise and bot 5xx); the exact reported 500 was
not visibly logged.

## What Open WebUI serves without a session (should be pre-auth)

From `backend/open_webui/main.py` and the FastAPI static mount:

- OIDC SSO flow (handled by `backend/open_webui/utils/oauth.py`):
  - `/oauth/oidc/login`
  - `/oauth/oidc/login/callback`
  - `/oauth/oidc/callback` (legacy)
- PWA / search metadata:
  - `/manifest.json`
  - `/opensearch.xml`
- Health / readiness probes:
  - `/health`
  - `/ready`
  - `/health/db`
- Static assets: `/static/` (public FastAPI static mount)

Open WebUI's configured OIDC redirect URI is exactly
`https://chat.<domain>/oauth/oidc/callback` (from
`config/ansible/app/files/authentik-manager`, `write_owui_blueprint`).
This mirrors Zulip's existing `/api/` pre-auth exemption
(`config/ansible/app/files/zulip-locations.conf`).

## Work already done (repo change — applied in the cluster, uncommitted)

`config/ansible/admin/files/open-webui-locations.conf` now exempts the
pre-auth surface from the edge gate by adding **two locations before** the
gated `location /`:

```nginx
location ^~ /static/ {
    error_page 500 502 503 504 /50x.html;
    proxy_pass http://127.0.0.1:(( port ))/static/;
    proxy_set_header Host $host;
    ...
}

location ~ ^/(oauth/.*|manifest\.json|opensearch\.xml|health|ready)(/.*)?$ {
    error_page 500 502 503 504 /50x.html;
    proxy_pass http://127.0.0.1:(( port ));
    proxy_set_header Host $host;
    ...
}
```

The regex exempts all of Open WebUI's OAuth protocol namespace (`/oauth/*`,
any provider) plus PWA/search metadata and health probes. `health` covers
`/health/db` via the trailing `(/.*)?`, so `health/db` is not listed
explicitly. All other `chat.<domain>` traffic stays behind
`auth_request /outpost.goauthentik.io/auth/nginx`. The regex is matched
against the path (query strings are fine). The comment block in the file was
updated to document the exemption.

Verified against OWUI's routes. Status: applied in the cluster (ops record in
gitignored `NOTES.md`), `nginx -t` passes. Nothing committed (repo rule: no
commits unless asked).

## Remaining tasks

1. **Validate in practice** — confirm the OIDC round-trip / health probes no
   longer go through the outpost and there are no new regressions; watch for
   the daily-kick / 500 reports going quiet.
2. **Follow-up — edge daily kick: RESOLVED.** The refresh-token
   half of the daily-kick root cause was fixed earlier (#2,
   `offline_access` + `refresh_token_validity` on `provider-owui` — see
   `owui-oidc-refresh-token.md`). The edge half is now fixed too:
   `open-webui-edge` (`provider-chat`) `access_token_validity` raised from
   `days=1` to `days=30` to match the OWUI runtime `auth.jwt_expiry=30d`
   (not the default `4w`; check `openwebui.config` for the runtime value).
   Deployed via `app/install-authentik.yml --limit <leader>` and verified
   live in `authentik_providers_oauth2_oauth2provider`. Existing sessions
   that already expired still needed one manual re-login; after that, edge
   and app session lifetimes align.
3. **Keep docs/comments consistent:**
   - `config/ansible/app/files/open-webui.conf.j2` still implies all
     `chat.<domain>` bytes are edge-gated before the app; that's now less
     than fully true.
   - Consider a note in `docs/design/perimeter-software-tracking.md` that
     OWUI's pre-auth/SSO paths are exempt, similar to Zulip `/api/`.

## Refs

- `config/ansible/admin/files/open-webui-locations.conf` (the staged change)
- `config/ansible/app/files/open-webui.conf.j2`
- `config/ansible/app/files/authentik-manager` (`open-webui-edge`,
  `write_owui_blueprint`)
- `config/ansible/app/files/zulip-locations.conf` (precedent: `/api/` exempt)
- `config/ansible/admin/setup-web-services.yml` (deploys the locations file)
- `docs/operations/ansible.md`, skill `deploy-to-cluster`
