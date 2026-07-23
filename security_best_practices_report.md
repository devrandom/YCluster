# YCluster Security Best Practices Review

**Scope:** Python Flask admin web app (`config/ansible/admin/files/app.py`, `health-api.py`) + minimal vanilla JS frontend (Alpine.js + `nav.js`) + nginx reverse-proxy configs.
**Method:** Active audit per `python-flask-web-server-security.md` and `javascript-general-web-frontend-security.md`.

## Executive Summary

The ycluster admin surface is well-architected from a security standpoint: nginx enforces subnet ACLs, method allowlists, and rate limits at the edge; Flask uses nginx-set forward-auth headers (no cookies, no `SECRET_KEY`); the belt-and-suspenders `_authentik_identity()` rejects smuggled headers from non-loopback callers. The findings below are mostly defense-in-depth and small refactors — no critical vulnerabilities. The most actionable item is replacing `app.run()` in `health-api.py` with a production WSGI server (matches what `app.py` already does with waitress).

## Findings

### M1 — FLASK-DEPLOY-001: `app.run()` (Flask dev server) used in production

**Severity:** Medium
**Location:** `config/ansible/admin/files/health-api.py:1177`
**Evidence:**
```python
if __name__ == "__main__":
    # Bind on all interfaces so admin-api on storage peers can fetch
    # each node's health cross-node via http://<ip>:12724/api/health.
    app.run(host="0.0.0.0", port=HEALTH_API_PORT)
```
This service runs on every managed node via systemd (see `config/ansible/admin/setup-web-services.yml:139`). The Werkzeug dev server is not designed for cross-node production traffic.
**Impact:** Performance/stability rather than security (the endpoint is read-only and behind nginx), but it's still a deviation from the project's own pattern.
**Fix:** Use `waitress.serve(app, host='0.0.0.0', port=HEALTH_API_PORT, threads=8)` (matches `app.py:3476`).
**Mitigation:** The endpoint is no-auth and nginx-restricted; no immediate security risk.

### M2 — FLASK-XSS-001 / JS-XSS-001: `x-html` used to render server-supplied error strings

**Severity:** Medium (defense-in-depth)
**Locations:**
- `config/ansible/admin/files/templates/status.html:336,358` — `<td x-html="getClockSkewOffset(host.hostname)"></td>`
- `config/ansible/admin/files/templates/status.html:722` — `return \`<span style="color: #666;">${clockSkew.details.message}</span>\`;`
- `config/ansible/admin/files/templates/inventory.html:250,259` — `this.statusMessage = \`...Error: ${data.error || r.statusText}...\``

**Evidence:** These interpolate server-returned strings into HTML via Alpine's `x-html` directive (which calls `innerHTML`). The strings originate from `str(exception)` paths in `check_clock_skew()` (`app.py:857-871`) and from `inventory_set_asset` error returns.

**Impact:** Currently low because:
- The NTP server is hardcoded (`10.0.0.254`), so `clockSkew.details.message` only reflects a trusted local NTP response.
- The `inventory` page is admin-only (forward-auth gated), and `data.error` is a short static string or `str(exception)`.
- Hostnames are regex-validated server-side before any display.

The risk is defense-in-depth: any future exception message that contains attacker-influenced HTML (or a regression in error-string handling) becomes a stored XSS on an authenticated admin page.

**Fix:** Replace `x-html` with `x-text` (Alpine's textContent equivalent, escapes automatically) where the rendered value doesn't actually need HTML formatting. If HTML is required, split static markup from dynamic text — e.g., render the colored `<span>` via a class binding and put the message in a sibling text node.

**Mitigation:** No immediate risk; address opportunistically.

### M3 — JS-CSP-001: No Content-Security-Policy on admin pages

**Severity:** Medium
**Locations:** `config/ansible/admin/files/admin-api.conf.j2`, `config/ansible/admin/files/ycluster_app.conf.j2`, `config/ansible/admin/files/open-webui.conf.j2`, `config/ansible/admin/files/authentik_auth.conf.j2`

**Evidence:** Nginx sets `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options nosniff`, `X-XSS-Protection` — but **no `Content-Security-Policy`** anywhere. A `rg -n "Content-Security-Policy" config/ansible/` returns zero hits.

**Impact:** CSP is the primary defense-in-depth against XSS. Without it, a single HTML-injection bug (e.g., the `x-html` sites in M2) escalates directly to script execution. The other headers don't substitute for it.

**Fix:** Add a strict CSP via `add_header` in the nginx vhosts. Since the app uses Alpine.js + nav.js served from same-origin `/static/`, a tight policy is straightforward:

```nginx
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" always;
```

Notes:
- `'unsafe-inline'` for `style-src` is required because Alpine sets some inline styles (or use hashes).
- `'self'` covers the same-origin `/static/alpine-3.14.1.min.js` and `/static/nav.js`.
- `frame-ancestors 'none'` duplicates the `X-Frame-Options DENY` but is the modern replacement.

**Mitigation:** Defense-in-depth; no immediate exploit.

### L1 — JS-XSS-001 (defensive): `nav.js` uses `innerHTML` with constant data

**Severity:** Low
**Location:** `config/ansible/admin/static/nav.js:66`
**Evidence:**
```js
nav.innerHTML = '<span class="brand">...' + brand + '</span>' + items.map(function (it) { ... }).join('');
```
**Impact:** Today this is safe because `brand` and `items` are hardcoded constants derived from `location.hostname.startsWith('app.')`. No user-controlled data flows in. Future changes that start passing dynamic content into these arrays would introduce XSS without changing this line.
**Fix:** Refactor to use `document.createElement` + `textContent`/`appendChild` (the script is small). This eliminates the sink so future edits can't accidentally introduce XSS.
**Mitigation:** Acceptable as-is; refactor for defense-in-depth.

### L2 — FLASK-HTTP-001: State-changing actions exposed via GET

**Severity:** Low
**Locations:**
- `config/ansible/admin/files/app.py:382` (`/api/allocate`) — `get_or_create_allocation()` may create new etcd records. Triggered by PXE/GRUB during network boot.
- `config/ansible/admin/files/app.py:469` (`/api/wg/poll/<hostname>`) — leaks approved peer config when fingerprint matches.

**Impact:** Both are intentional for the bootstrap flow (GRUB does GETs, wg peer polls do GETs). `/api/allocate` is LAN-only (subnet ACL in nginx). `/api/wg/poll` requires the `fp` query parameter to prevent enumeration (`app.py:478-484`).

**Fix:** Document the GET-with-side-effect design choice in code comments; rate-limit `/api/allocate` if it isn't already (`rg "limit_req" admin-api.conf.j2` shows none). Optional: move `/api/allocate` to POST with `X-Authentik-*` headers (still LAN-only) — not worth the PXE integration cost.

**Mitigation:** Current design is acceptable; rate-limit `/api/allocate` as a hardening step.

### L3 — FLASK-LIMITS-001: No app-level request size limit

**Severity:** Low
**Locations:** `config/ansible/admin/files/app.py` (no `MAX_CONTENT_LENGTH`), `config/ansible/admin/files/health-api.py` (no `MAX_CONTENT_LENGTH`).

**Impact:** The nginx default (`client_max_body_size 1m`) covers POST bodies. `ycluster_app.conf.j2:19` sets `1M` explicitly. admin-api relies on the default. The app-level limit is defense-in-depth in case nginx config drifts.

**Fix:** Add `app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024` to both apps, mirroring the nginx limit.

**Mitigation:** Acceptable as-is; nginx is the controlling limit.

## Positive Findings (defenses in place)

These are good patterns worth keeping — flagged so a future refactor doesn't accidentally regress them.

- **No Flask sessions.** Auth uses nginx-set `X-Authentik-UID` / `X-Authentik-Email` / `X-Authentik-Groups` headers, not cookies. No `SECRET_KEY`, no CSRF tokens needed. (`app.py:2799-2850` — `_authentik_identity()`)
- **Belt-and-suspenders auth.** `_authentik_identity()` requires `remote_addr` ∈ `127.0.0.0/8` AND headers set by nginx. The headerless "operator-space" shortcut is gated on `remote_addr == "127.0.0.1"` exactly, so a remote cluster node can't smuggle admin headers. (`app.py:2837-2849`)
- **Hostname validation regex.** `HOSTNAME_PARAM_RE` (`app.py:127`) blocks crafted URL params from addressing adjacent etcd namespaces; the `validated_hostname` decorator is applied to all `<hostname>` / `<target_hostname>` routes.
- **WG pubkey validation.** `wg_register` (`app.py:442-447`) requires 32-byte base64 with `validate=True` before any etcd write.
- **Nginx subnet ACL.** `admin-api.conf.j2:13-16, 61-64` rejects non-cluster traffic with `deny all`, even though the app is intended to be LAN-only.
- **Nginx method allowlists.** Every public location uses `limit_except GET { deny all; }` (and `GET HEAD POST` for authed mutating endpoints).
- **Rate limiting.** `limit_req` zones on every public and authed location in nginx.
- **Public/whitelist split.** `admin-api-public-locations.conf` is the only path reachable from the internet via rathole; everything else falls through to subnet-restricted `location /`.
- **WG poll enumeration blocked.** `/api/wg/poll/<hostname>` requires a `fp` query param matching the peer's `pubkey_sha256` (`app.py:483`).
- **Subprocess safety.** All `subprocess.run` calls use lists (no `shell=True`); `service_name` callers pass hardcoded constants.
- **SQL safety.** `app.py:2743` builds the `WHERE` structure with an f-string but binds all values via `%s` parameters (`app.py:2735-2742`).
- **Path safety.** `send_from_directory` (Werkzeug `safe_join`) is used for `/static/<path:filename>` (`app.py:2583`).
- **SSRF surface.** All `requests.get` URLs are hardcoded constants (cluster-internal VIPs, localhost) — no user-supplied URL fetching.
- **SSTI surface.** Two `Template()` calls (`app.py:1584, 1680`) render hardcoded `.j2` files with hardcoded variable values — no user-controlled template strings.
- **Open redirect.** No `redirect(request.args.get(...))` patterns.
- **Third-party JS.** Alpine.js + nav.js are self-hosted at `/static/` (no CDN), so SRI is not required. No third-party scripts.
- **DOM clobbering.** No `window.foo || default` patterns in `nav.js` or templates.
- **localStorage.** Not used; no JS-readable session storage.

## Recommended Action Order

1. **M1** — replace `app.run()` with `waitress.serve` in `health-api.py`. One-line change, mirrors `app.py`.
2. **M3** — add `Content-Security-Policy` header to the three nginx vhosts that serve HTML (`admin-api.conf.j2`, `ycluster_app.conf.j2`, `open-webui.conf.j2`). One `add_header` line each.
3. **M2** — swap `x-html` → `x-text` in the two templates where the rendered value doesn't need HTML formatting (defense-in-depth).
4. **L1/L2/L3** — opportunistic refactors.

## Out of Scope / Not Reviewed

- Ansible playbooks (YAML/Shell) — not a security-best-practices target for this skill.
- `local-ai-proxy` (separate Flask app, not in this scan).
- Authentik/PostgreSQL admin endpoints (operated by their own apps, not in this codebase).
- Infrastructure-level concerns (firewall rules, SSH keys, etcd ACLs).
