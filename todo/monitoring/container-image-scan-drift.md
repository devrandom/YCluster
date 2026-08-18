# Container image scan-drift detection

Detect when the scan input (the `osv-feed` container manifest snapshot at
`/near1/osv-feed/container/*.manifest`) has silently diverged from the image
actually running, so `PerimeterCVE*` alerts describe the running surface
rather than a stale snapshot.

Priority rationale: **MEDIUM**. A recent authentik incident showed the
failure mode (bumped image deployed, stale 2026.2.4 manifest still scanned →
two `PerimeterCVECriticalLongPending` false positives), but today's
`PerimeterCVE*LongPending` alerts still *surface* the mismatch indirectly —
an operator reviewing the firing alerts sees the stale version and can
re-push `container-manifests/`. So this hardens alert fidelity (catches both
directions, including the silent false-negative case) rather than fixing a
currently unmonitored hole.

Design: `docs/design/container-image-drift.md`. Tracked images:
authentik, open-webui, zulip.

## Work

- Extend `osv-feed`:
  - `osv_feed/container.py` — parse zero or one `@image` directive per
    manifest (spec lines unchanged, so CVE matching is unaffected).
  - `osv_feed/manifestgen.py` — emit the directive when the caller supplies
    the identity, so it rides along in the committed manifest.
- Storage-leader textfile collector: iterate every `*.manifest`, parse its
  `@image` directive, `docker inspect` the same-named container, compare.
  Generic — no image list, no per-image branch. Image name derives from the
  manifest file name, identity travels inside the manifest.
- Alert `PerimeterImageOutdated` (name already reserved in
  `docs/design/container-image-tracking.md`), generic labels
  (`image` / `expected` / `actual`), fires on drift, with an `unverifiable`
  dimension for a manifest with no directive or a declared image that isn't
  running.
- Build-side: stamp `org.opencontainers.image.version` / `.revision` on the
  in-cluster builds (`build-open-webui.yml`, `build-zulip.yml`); authentik
  already has a comparable digest.
- Tests: directive-parse + compare pure functions, `promtool check rules`,
  dev-cluster fixtures for both drift directions plus a newly-added image
  requiring zero collector changes.

## Out of scope

- comfyui (no manifest) — stays unscanned until opted in by adding a manifest.
- Upgrade cadence — running a consistent-but-older image is normal flow, not
  drift.
