# Receipt Wrangler (receipt capture / expense tracking)

Receipt Wrangler is the cluster's receipt manager: photo/PDF capture with
OCR + LLM parsing, groups with per-user roles, splitting, and CSV/report
exports. It runs leader-gated on the storage leader, like Authentik, Forgejo
and Vikunja.

**Status:** deployed on the storage leader with external access enabled:
`https://receipts.<domain>` (web) and the mobile apps pointed at the same
URL. Internal access remains at `http://wrangler.xc`.

Design boundaries:

- **Backend:** upstream `noah231515/receipt-wrangler` image, pinned by amd64
  digest and copied into `registry.xc`. It bundles the Go API, the Angular
  web UI, nginx, Tesseract/EasyOCR and Chromium.
- **Metadata:** shared PostgreSQL, database and role `wrangler`.
- **Queue:** Redis sidecar (per-install, `requirepass`), because Redis backs
  the OCR/email/scheduled-task job queue and the app refuses to start
  without it.
- **Files:** local filesystem under `/rbd/user/wrangler` (`data/`, `logs/`,
  `redis/`).
- **Identity:** Authentik OIDC at `receipts.<domain>`, SSO with
  first-login provisioning. The public vhost is SSO-only: `POST /api/login`
  returns 403 there and the password form is hidden, while the internal
  `wrangler.xc` vhost keeps password login as break-glass.
- **External URL:** `https://receipts.<domain>` (DNS record is out of band).
- **Internal URL:** `http://wrangler.xc` on the storage VIP.
- **No edge forward-auth:** Wrangler is its own OIDC/API credential
  boundary. The external vhost is a pre-auth surface and is tiered P0-U in
  `perimeter_software.yml`.

## Files and units

| Item | Path |
|---|---|
| Playbook | `config/ansible/app/install-wrangler.yml` |
| Compose | `/etc/wrangler/docker-compose.yml` |
| Manager | `/usr/local/lib/cluster/wrangler-manager` |
| Bootstrap helper | `/usr/local/lib/cluster/wrangler-bootstrap` |
| Data | `/rbd/user/wrangler` |
| Unit | `wrangler.service` (`ycluster-apps.target`) |
| Internal vhost | `wrangler-internal` (`wrangler.xc`) |
| External vhost | `wrangler_receipts` (`receipts.<domain>`) |
| Secrets | etcd `/cluster/config/wrangler/*` and `/cluster/config/authentik/wrangler-client-secret` |

`wrangler-manager` reads the secrets from etcd into a 0600 env file in
`/dev/shm` at every start (never argv — the IDS logs command arguments) and
runs the compose stack. It also rotates the bootstrap `admin/admin` account
the app creates on an empty users table, so a fresh database cannot stay on
the default password.

## Deploy

```bash
cd /etc/ansible
./run-playbook.sh app/install-authentik.yml --limit storage
./run-playbook.sh app/install-wrangler.yml --limit storage
```

The playbook defaults to internal-only (`http://wrangler.xc/` with local
password login). This deployment runs with `-e wrangler_external=true`; the
admin password is the etcd value below.

### Enabling external access (mobile app, off-cluster browsers)

External access is DNS-first. Do not enable the vhost before the name
resolves: `certbot-manager` adds `receipts.<domain>` to the shared
certificate SAN list, and a record that does not resolve fails the next
renewal for **every** vhost. Per-host wildcard DNS covers `receipts.<domain>`
in this cluster, so no per-name record is needed here.

1. Confirm `receipts.<domain>` resolves (wildcard or explicit record).
2. Redeploy with external access on:

```bash
./run-playbook.sh app/install-wrangler.yml --limit storage \
  -e wrangler_external=true
# on the storage leader:
ycluster certbot obtain
```

That also sets the app's `serverPublicUrl`, registers the `authentik` OIDC
provider with first-login provisioning, and lets `wrangler-manager` read the
Authentik client secret. The mobile app connects with the server URL
`https://receipts.<domain>` (the app appends `/api`).

Turning it back off (`-e wrangler_external=false`) removes the vhost and
leaves local auth available.

## AI extraction

Extraction is configured by a `receipt_processing_settings` row plus the
`receipt_processing_settings_id` pointer in `system_settings`. Both live in
the database only — the playbook does not create or overwrite them, so a
restore from an empty database needs the one-time setup again (admin UI, or
the two API calls below).

This deployment runs:

| Field | Value |
|---|---|
| AI type | `OPEN_AI_CUSTOM` (OpenAI-compatible base URL) |
| URL | `http://inference.xc/v1` (base URL; the client appends `/chat/completions`) |
| Model | `deepseek-v4_1-flash` (multimodal; verified it accepts `image_url`) |
| OCR | not used (`isVisionModel=true`); Tesseract stays configured only for a switch back |
| Prompt | `1` (Default Prompt) |
| JSON mode | enforced |
| Gateway key | etcd `/cluster/config/wrangler/inference-key`, owner `receipt-wrangler@ycluster.invalid` |

The container reaches the gateway because `wrangler-compose.yaml` pins
`inference.xc` in `extra_hosts`; Docker's resolver does not know `*.xc`. The
pin is `host-gateway`, not the storage VIP: both services are on the leader,
so the call stays on the host bridge instead of crossing the cluster network,
and the gateway nginx already accepts `172.16.0.0/12`. The gateway
authenticates the Bearer key against its etcd
key store and injects `X-User-Id` for `local-ai-proxy`, so routing, health,
cancellation and the per-user concurrency caps all apply. The key is minted
with `ycluster inference key create` and can be revoked with
`ycluster inference key revoke <key-id>`.

Pipeline, end to end: upload/email → the image itself (Tesseract text only
when `isVisionModel` is false) → `@ocrText`/`@categories`/`@tags`/`@currentYear`
substituted into the prompt → chat completion → JSON into the receipt →
Magic Fill review or Quick Scan save. Every stage writes a `system_tasks` row
with its raw input/output, so a bad field is traceable to OCR or to the
model.

Fallback settings are only tried when the primary fails (error or
unparseable output), not when it returns a plausible but wrong value. Keep
Magic Fill (review before save) for new receipt shapes.

## First login and IdP recovery

Read the bootstrap admin password on the storage leader:

```bash
set -a; . /etc/ycluster/etcd-client.env; set +a
etcdctl get /cluster/config/wrangler/admin-password --print-value-only
```

OIDC users are provisioned on first login (provider `authentik`,
`allowProvisioning: true`). The Authentik application is not group-bound by
default: any IdP user who can reach it can create a Wrangler account. Bind
the provider to a group policy if that is not intended.

If the IdP is unavailable, sign in with the local `admin` account. Rotating
that password inside the UI leaves the etcd value stale; to reset it, delete
`/cluster/config/wrangler/admin-password` and restart the unit — the manager
generates a new one, but the app only accepts it for the *bootstrap* account
while its password is still `admin`. Reset the password in the UI instead,
then update the etcd value by hand.

Break-glass login is `http://wrangler.xc/` (cluster network only). The
external vhost refuses `POST /api/login` and `/api/login/` with 403, and the
Angular login page's password form is hidden there via a `sub_filter` style
injection; the OIDC button remains. The API on `127.0.0.1:9082` is not
filtered, so `wrangler-manager` can still rotate the bootstrap password.

## Health and logs

```bash
systemctl status wrangler.service
cd /etc/wrangler && docker compose -p wrangler ps
journalctl -u wrangler -n 200 --no-pager
cd /etc/wrangler && docker compose -p wrangler logs --tail=200 wrangler
curl -fsS http://127.0.0.1:9082/
```

Host nginx is the only web ingress; the container's port 80 is published on
loopback only.

## Security posture

Receipt Wrangler 7.3.0 is the pinned release. The vendored
`wrangler.manifest` currently matches 125 OSV advisories across 9 packages:
Pillow 59 (easyocr's image decoder, i.e. reachable from uploaded receipts),
Excelize 30 (XLSX parsing), filelock 12, pip 12, setuptools 5, `go-jose` 3,
`golang.org/x/net` 2 (the HTML mail parser used by email integration), chi 1
(client-IP spoof) and `x/crypto` 1. None of the reviewed Go findings is RCE;
most are panic/DoS or parser hardening, but Pillow's count is the one to
re-check on every bump.

The desktop app's npm tree has older MCP SDK and `undici` advisories; the
server image does not run it.

Mitigations that matter here: keep the email-integration address private (the
HTML parser is the reachable surface), do not import untrusted XLSX files,
and re-run the lockfile scan when bumping the image. The image also carries a
Debian apt layer that `manifestgen` does not cover yet; the vendored
`wrangler.manifest` tracks the Go buildinfo and Python venv only.
