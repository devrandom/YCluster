# Container Image CVE Tracking

## Why

The perimeter update plan (`docs/design/perimeter-software-tracking.md`)
splits the externally reachable surface into apt/source packages (already
covered by `osv-feed`, see `docs/design/osv-feed.md`) and the **public app
container images** that sit on the P0-U pre-auth / P0-A authenticated
boundary. Those images are the remaining uncovered slice of "track CVEs
for all perimeter software": a CVE in the base Ubuntu packages, the Python
(uv/pip) dependencies, or the Node frontend of an image translates into a
vulnerability on the public login/pre-auth path, but no current mechanism
observes it.

## Status

- **P1 (osv-feed capability) — done in repo.** The `container` category and
  `match --container-dir` manifest matcher landed in `osv-feed/` with
  server-free tests (manifest fixture → expected findings, per-image
  first-seen, prometheus `image` label); a bare `match` run plus
  `--container-dir` covers host + container CVEs in one collector pass.
- **P2 (deploy wiring) — repo-landed, not deployed.** `install-osv-feed.yml`
  mirrors `PyPI` alongside Ubuntu/Debian/Go, publishes its cursor, adds
  `[tracking.container]` to the collector config, points the collector at
  `OSV_FEED_CONTAINER_DIR` (`/near1/osv-feed/container`), and now syncs
  `config/ansible/monitoring/container-manifests/*.manifest` into that shared dir. The pending-CVE
  alerts (`PerimeterCVE*LongPending`) join on `image` so container images
  stay distinct perimeter surfaces. Deploying to the live collector is
  blocked until prod access is re-enabled.
- **P2/P3 (manifests, open-webui/zulip/authentik) — done + verified.** A
  stdlib-only lockfile→manifest generator
  (`osv-feed/osv_feed/manifestgen.py`, parses `uv.lock`) with a runtime
  `--uv-select` filter (`all`/`base`/`<group>`) so dev-only tooling (black,
  pytest, ruff, daphne, mypy…) never ships in a runtime manifest. Vendored
  manifests:
  - `config/ansible/monitoring/container-manifests/open-webui.manifest` — **265** PyPI packages from
    open-webui v0.11.3 / `2a960a59` with `--uv-select base` (production deps).
  - `config/ansible/monitoring/container-manifests/zulip.manifest` — **182** PyPI packages from zulip
    12.1 / `7d85fc15` with `--uv-select prod` (mirrors upstream
    `create-production-venv`, which runs `uv sync --only-group=prod`).
  - `config/ansible/monitoring/container-manifests/authentik.manifest` — **158** PyPI packages from
    authentik 2026.8.5 / `4ec0d19b98` with `--uv-select base` (web frontend is
    baked at build).
  All three re-matched end-to-end against a real PyPI mirror with no dev
  tooling present (see per-image results below).
- **P3 (authentik digest pin) — done + deployed.** `app/install-authentik.yml`
  pins the amd64 server image by digest
  (`ghcr.io/goauthentik/server@sha256:6b924afb…`, var `authentik_image`), cached
  under a digest-suffixed registry tag
  (`registry.xc:5000/authentik:2026.8.5-<digest>`);
  `files/authentik-compose.yaml` lets `AUTHENTIK_IMAGE` override the whole
  reference (falls back to `:tag`), and `authentik-manager` writes it to the
  compose env like the tag.
- **P4 — partially open.** `PerimeterImageOutdated` (running-ref ≠ pinned-digest
  drift needs a collector-fed expected-digest metric) remains; the runbook entry
  has landed (see the CVE/container section of `docs/operations/packages.md`),
  pending only the post-deploy close-out of the "containers" bullet in
  `todo/security/cve-tracking.md`.


## Current image inventory (ground truth)

| Image | Registry ref today | Source pin | Tier | Runs on |
|---|---|---|---|---|
| authentik server+worker | `registry.xc:5000/authentik:2026.8.5-6b924afb9705` | `ghcr.io/goauthentik/server@sha256:6b924afb…` (var `authentik_image`) + `authentik_version: "2026.8.5"` in `app/install-authentik.yml` | P0-A (IdP + authenticated web) | storage nodes |
| open-webui | `registry.xc:5000/open-webui-with-plugins:latest` | upstream v0.11.3 `2a960a59…` + mobile-upload/NLTK patches and plugins SHA `746f4b2f…` in `app/build-open-webui.yml` | P0-U (login/static/pre-auth) | storage leader |
| zulip | `registry.xc:5000/zulip:12.1-0` | docker-zulip `9a0bd360…` + zulip src `7d85fc15…` in `app/build-zulip.yml` | P0-U (login/registration/pre-auth) | storage leader |

open-webui and zulip are built in-cluster from pinned source
(`build-*.yml`, no prebuilt images); authentik was previously an upstream
image pulled by mutable tag (`:2026.2.4`) and is now digest-pinned via
`authentik_image` in the service unit (repo-landed, not yet deployed).

## Per-image CVE findings (real PyPI mirror, current manifests)

Re-matched from the vendored `config/ansible/monitoring/container-manifests/`
against a populated PyPI
mirror. These are the pending findings that feed the `PerimeterCVE*LongPending`
SLA once deployed; regenerate manifests and re-run after any image bump.

| Image | findings | critical | high | no_fix_yet | affected packages |
|---|---|---|---|---|---|
| open-webui | 248 | 1 | 61 | 3 | aiohttp, chromadb, cryptography, httplib2, mcp, nltk, pillow, pyarrow, pyasn1, pymdown-extensions, pypdf |
| zulip | 84 | 0 | 21 | 0 | cryptography, msgpack, pyasn1, pyjwt, soupsieve, tornado |
| authentik | 5 | 0 | 5 | 0 | aiohttp, urllib3 |

Notable unresolved surface: authentik 2026.8.5 clears the prior
cryptography/pyopenssl criticals (`PYSEC-2026-36`, `PYSEC-2026-2269`) and
now carries five high aiohttp/urllib3 findings with fixes; open-webui carries
a `no_fix_yet` critical (chromadb `GHSA-f4j7-r4q5-qw2c`) plus two nltk highs.


## Vulnerability surface per image

An image's CVE surface is the union of things OSV indexes:

- the base OS layer (Ubuntu/Debian `apt` packages — open-webui `USE_SLIM`
  builds and the docker-zulip image do `apt` work during build),
- Python dependencies (uv/pip + transitive pins): open-webui backend,
  zulip Django server, authentik (Python),
- the Node/webpack frontend (open-webui Svelte; zulip serves a baked
  bundle),
- any Go component (e.g. export/import workers) — `Go` ecosystem.

All of these map to OSV ecosystems (`Ubuntu`/`Debian`, `PyPI`, `npm`,
`Go`) that the local `osv-feed` mirror already, or can cheaply, index.

## Constraints

- **No prebuilt third-party binaries.** Do not `get_url` a scanner binary.
  Tooling must be built from pinned source (`go install` at a pinned SHA)
  or be stdlib-only and live inside `osv-feed/`.
- **Leak-free.** Image manifests and digests are internal cluster state;
  keep them on the collector/`/near1`, never uploaded.
- **Reuse the existing model.** Findings should flow through the SAME
  pipeline as apt/source packages: local mirror → `match` →
  `osv_feed_cve_info` (`category="container"`) → `first_seen.json` →
  `PerimeterCVE*LongPending`, so SLA, ack, and routing all work unchanged.

## Recommended approach (osv-feed-native)

Rather than pulling an external image scanner, extend osv-feed to match an
**explicit `ecosystem:name=version` manifest per image** against the local
mirror — the same `name=version` spec form already used for the self-built
`frp` pin. This keeps the whole path inside the existing, tested facility
and avoids the "what was scanned vs what runs" drift that a separate
scanner introduces.

1. **Emit a manifest at build/publish time.** For source-built images
   (open-webui, zulip) generate from the pinned tree's lockfiles/manifests
   at the pinned SHA (`uv.lock` / `requirements.txt` / `package-lock.json`
   / `package.json`, base-image `apt` list), so the manifest is exact for
   the SHA we built. For authentik, generate by resolving its published
   image (layers/base + `python -m pip list` dump) at the pinned tag.
   Store as `container/<image>-<digest>.manifest` next to the OSV snapshot
   on `/near1`.
2. **Mirror the ecosystems the images need** on top of the existing
   Ubuntu/Debian/Go mirror: `PyPI` (~32 MB) for the Python runtime surface
   of every image here. The web frontends are baked into static assets and
   `node_modules` is never shipped in the runtime image, so `npm` is
   intentionally out of scope for open-webui/zulip (vendoring it would be
   build-time noise); add it back only if a future image runs Node at
   runtime.
3. **Match as a `[tracking.container]` category.** osv-feed gains a
   container match mode that reads each `container/*.manifest` and matches
   every `ecosystem:name=version` line against the local records, emitting
   `osv_feed_cve_info{category="container", image="…", …}`.
4. **Digest-pin what runs.** Record the image digest (and, for authentik,
   move `:2026.2.4` to a `@sha256:…` pin) at deploy so the scanned manifest
   corresponds to the running image. Add `PerimeterImageOutdated` for
   running-ref ≠ pinned-digest drift.
5. **Alerts reuse the existing rows.** The `[tracking.container]` findings
   ride the same `PerimeterCVE*LongPending` first-seen SLA; add a
   `PerimeterCVEHighContainerLongPending` only if container findings need a
   distinct routing window.
6. **Ack per surface, not per CVE.** osv-feed acks accept a `--surface`
   (a container name, or a non-container install such as `local-ai-proxy`)
   so a shared CVE can be acknowledged on one image after a per-image review
   without suppressing it on the others. An empty `--surface` remains the
   legacy wildcard. This replaces the "container ack silences every image"
   behavior and is what makes a per-image perimeter-CVE review actionable
   without cross-cutting suppression (`docs/operations/packages.md`).

## Phased rollout

- **P1 (osv-feed capability):** add `PyPI` to the mirror/cursor set and a
  `container` category + manifest matcher in `osv-feed/` with server-free
  tests (manifest fixture → expected findings). No prod change. **Done.**
- **P2 (manifests at build):** generate `container/*.manifest` from the
  pinned trees via `osv-feed/osv_feed/manifestgen.py` with `--uv-select`
  runtime filtering, vendored at `config/ansible/monitoring/container-manifests/` (open-webui `base`,
  zulip `prod`, authentik `base`) and synced to `/near1/osv-feed/container`
  by `install-osv-feed.yml`. **Done in repo** (regenerate and re-sync after
  each image/`uv.lock` bump).
- **P3 (authentik):** digest-pin `ghcr.io/goauthentik/server`
  (`authentik_image` var + compose override + manager). **Done in repo.**
- **P4 (alert + runbook):** `PerimeterImageOutdated`, and close-out of the
  "containers" bullet in `todo/security/cve-tracking.md`. The runbook entry has
  landed (`docs/operations/packages.md`); `PerimeterImageOutdated` and the
  post-deploy close-out remain. The core pending-CVE alerts already cover
  containers (image-aware join added).

## Open decisions (need owner input)

- Whether to add `PerimeterImageOutdated` (needs a collector-fed
  expected-digest metric reflecting the running ref vs. the pinned manifest
  digest).
- Whether the base OS `apt` surface inside each image should be tracked
  separately (mirrored `Ubuntu` specs for the base image) in addition to
  the PyPI runtime surface.
- Whether container findings need a distinct routing window
  (`PerimeterCVEHighContainerLongPending`) or can keep riding the shared
  `perimeter_cve` route.

