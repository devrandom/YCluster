# Dev cluster: browsable external/TLS path (make reproducible)

The forward-auth UX (redirect to authentik, login, bounce back) can't be
exercised in a browser against dev — the public/app vhosts bind
127.0.0.2 only (the rathole endpoint, no analog on dev) and render
HTTP-only (`web_services_force_local`), while authentik builds redirects
as `https://<sub>.<domain>/...`.

Headless curl coverage exists (cookie round-trip in the step-3 work); the
app.<domain> work (2026-06-13) was validated end-to-end on dev only via
**ad-hoc scaffolding** (a `.xc` override of the app provider's
`external_host` + outpost `authentik_host`, plus a node-interface
`app.xc` nginx vhost) — that scaffolding is NOT reproducible and
`authentik-manager write-blueprints` resets the blueprint override, so
this needs a proper fixture.

## Concrete design

1. **Drop the `.xc` override** — point the operator's `/etc/hosts` at the
   *real* dev subdomains (`app.<dev-domain>`/`auth.<dev-domain>`/
   `admin.<dev-domain>` → dev leader / storage VIP) so nginx server-names,
   the authentik providers, and the cert all already agree (no drift
   after `write-blueprints`).
2. **Node-interface listen** — add a `listen <node>:443 ssl` to the public
   + app vhosts gated to the dev/`web_services_force_local` case (small
   dev-only template conditional) so the subdomains are reachable off
   127.0.0.2.
3. **TLS via the existing pebble ACME** (`dev/cluster/acme-dev.yml`
   already stands up a local CA) so the dev names get a dev-trusted
   cert.

Package as a `dev/cluster/` playbook + a doc line. **NB the dev leader is
s2 (not s1), and the storage VIP (10.0.0.100) follows it** — `/etc/hosts`
should target the VIP, not a fixed node.
