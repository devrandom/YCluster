# OWUI OIDC: issue refresh tokens so Open WebUI stops dropping its session daily

Part #2 of the Open WebUI edge/session investigation
(the nginx pre-auth carve-out is #1-track — see
`authentik/open-webui-edge-pre-auth-paths.md`). This one fixes the
**app-level OWUI session** being deleted daily because Open WebUI can't
refresh its 1-day access token from authentik.

**Status: applied and verified in the cluster.** (Operational/deploy record
lives in the gitignored `NOTES.md`, not this public repo file.) Repo change
uncommitted (repo rule: no commits unless asked).

## Symptom / evidence

Open WebUI (`chat.<domain>`) runs OIDC against the cluster IdP's
`open-webui` OAuth2 provider. That provider used `access_token_validity:
days=1` and only advertised `openid email profile` scopes — **no
`offline_access`** (`config/ansible/app/files/authentik-manager`,
`write_owui_blueprint()` → `provider-owui`).

Storage-leader OWUI logs show exactly the failure this causes:

```
No refresh token available for session ...
Failed to refresh token ... deleting session   (provider oidc, user 9f841097-...)
```

So when OWUI's 1-day access token nears expiry it tries to refresh, finds
no stored `refresh_token` (authentik never issued one), and deletes the
OAuth session → user is logged out of OWUI every 24h.

## Root cause (confirmed from OWUI source, v0.11.1-dr, vendored for the build)

The OWUI **client is already refresh-ready** — no client/env change needed:

- `backend/open_webui/utils/oauth.py:357` declares
  `grant_types=["authorization_code", "refresh_token"]`.
- `oauth.py:375-379` — when the client scope is unset, OWUI auto-requests
  **every scope the server advertises** in `scopes_supported`.
- `oauth.py:748-749` — refresh path warns `No refresh token available for
  session` when the stored token has no `refresh_token` (matches the log).

Because OWUI auto-requests whatever scopes_supported lists, the fix is
purely **provider-side**: make authentik advertise + grant `offline_access`
so the token response includes a `refresh_token`.

## Change applied (repo)

Edit `config/ansible/app/files/authentik-manager`, `write_owui_blueprint()`,
`provider-owui` entry:

1. Add the managed `offline_access` scope mapping to `property_mappings`
   (alongside the existing openid/email/profile):

   ```yaml
   property_mappings:
     - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
     - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
     - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
     - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-offline_access]]
   ```

2. Set `refresh_token_validity: days=30` explicitly (with a comment) so a
   refresh token is always available for the life of the OWUI ~4-week JWT
   (`JWT_EXPIRES_IN=4w`, default in `open-webui-manager`).

3. Leave `access_token_validity: days=1` as-is — the refresh flow makes the
   short access token moot at the OWUI layer.

No change to `open-webui-manager` / OWUI env: OWUI already requests whatever
the server advertises.

### Blueprint apply mechanics

`write_owui_blueprint()` writes the ycluster OWUI blueprint (a heredoc in
`authentik-manager`), which the authentik worker auto-applies on change; the
provided comment in the file notes "refreshing these needs no container
restart". `install-authentik.yml` has a `refresh authentik blueprints`
handler that runs `authentik-manager write-blueprints`. Adding a scope
mapping is an additive re-apply, so it doesn't clobber the existing provider.

## Verification

Done (see `NOTES.md` for the ops record):

1. The applied blueprint contains the `offline_access` scope mapping and
   `refresh_token_validity: days=30`.
2. The provider's OIDC discovery document
   (`…/application/o/open-webui/.well-known/openid-configuration`) now
   reports `scopes_supported` including `offline_access`.

Still worth a follow-up over the coming days:

3. A fresh authorization-code login should return a `refresh_token` in the
   token response.
4. Confirm the `default-provider-authorization-implicit-consent` flow does
   **not** add a surprise consent prompt for offline access (authentik treats
   `offline_access` as consent-relevant). If it does prompt, decide whether a
   one-time prompt is acceptable or adjust the flow.
5. **Existing sessions caveat:** users already logged in before the change
   have no stored refresh token, so they'll still be dropped once at the
   next 1-day boundary — expect ONE forced re-login, after which silent
   refresh keeps them in. (Sessions already deleted won't come back.)
6. Watch OWUI logs: the `No refresh token available` / `deleting session`
   lines should stop after re-login.

## Interaction / out of scope

- **Does NOT fix the edge daily kick.** The nginx forward-auth boundary
  (`open-webui-edge` proxy, `provider-chat`, `access_token_validity:
  days=1`) still bounces users every 24h regardless of OWUI's session. That
  is the remaining #1 follow-up (raise edge `access_token_validity` to match
  OWUI's ~4-week session) — tracked as the remaining task in
  `authentik/open-webui-edge-pre-auth-paths.md`. #2 only addresses the
  app-level (OWUI) session, not the nginx-gate session.
- No firewall / reboot / security-stability change.

## Refs

- `config/ansible/app/files/authentik-manager` (`write_owui_blueprint`)
- `config/ansible/app/install-authentik.yml` (manager copy → write-blueprints handler)
- `config/ansible/app/files/open-webui-manager` (OWUI env; client defaults)
- OWUI source `backend/open_webui/utils/oauth.py` (scopes + refresh logic)
- `authentik/open-webui-edge-pre-auth-paths.md` (part #1; this is part #2)
